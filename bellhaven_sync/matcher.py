"""Match website locations to CRM accounts and turn every difference into a proposal.

A proposal is one reviewable decision about one facility: what we found (evidence),
what we would change (actions), and why. Nothing here writes to the CRM.

Outcomes per website location:
  confident match  - one CRM account, already correct -> no proposal
  needs a fix      - wrong parent, outdated name, wrong ZIP, inactive -> PATCH
  CHOW             - needs a new parent but has revenue AND open AR -> keep the old
                     account untouched, create a new one, point chow_current_account at it
  duplicates       - several CRM accounts at the same street address -> keep one,
                     mark the rest duplicate_of + Inactive, move their contacts
  new location     - no CRM account -> create one under the Bellhaven parent
Plus, per CRM account under the Bellhaven parent that is no longer on the website:
  not on website   - flag Needs Review with a note, or CHOW-link it to the account a
                     new owner already has at the same address
"""
import hashlib
import json
from collections import defaultdict
from datetime import date

from . import normalize as N
from .config import NOTE_TAG

NEW_REF = "$new"  # placeholder for an account the proposal creates


def sop_blocks_reparent(acct):
    """Billing SOP: revenue history AND outstanding AR > 0 means the old account must be kept."""
    return (acct.get("lifetime_revenue") or 0) > 0 and (acct.get("outstanding_ar") or 0) > 0


def fingerprint(kind, subject, actions):
    """Stable ID for 'this exact change to this facility'. Notes are left out because
    they carry dates; everything else (targets and new values) is in."""
    core = []
    for a in actions:
        if a["op"] == "patch":
            core.append(["patch", a["account_id"], {k: v for k, v in sorted(a["set"].items()) if k != "note"}])
        elif a["op"] == "create":
            core.append(["create", {k: v for k, v in sorted(a["fields"].items()) if k != "note"}])
        elif a["op"] == "move_contact":
            core.append(["move_contact", a["contact_id"], a["to"]])
    raw = json.dumps([kind, subject, core], sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


class Matcher:
    def __init__(self, accounts, contacts, parent_name, today=None):
        self.today = today or date.today().isoformat()
        self.by_id = {a["account_id"]: a for a in accounts}
        parents = [a for a in accounts if a["name"] == parent_name]
        if len(parents) != 1:
            raise ValueError(f"Expected exactly one account named {parent_name!r}, found {len(parents)}")
        self.parent = parents[0]
        self.parent_id = self.parent["account_id"]

        referenced = {a["parent_id"] for a in accounts if a["parent_id"]}
        self.parent_ids = referenced | {a["account_id"] for a in accounts if a["name"].endswith("(Parent Account)")}

        self.contacts = defaultdict(list)
        for c in contacts:
            if c.get("is_active", True):
                self.contacts[c["account_id"]].append(c)

        # Accounts already pointing at another record (duplicate_of / chow_current_account)
        # are history, not candidates. This is what makes re-runs quiet after a decision.
        self.live = [a for a in accounts
                     if a["account_id"] not in self.parent_ids
                     and not a.get("duplicate_of_account")
                     and not a.get("chow_current_account")]

    # ------------------------------------------------------------------ helpers
    def note(self, acct, text):
        line = f"{NOTE_TAG} {self.today}: {text}"
        old = (acct or {}).get("note") or ""
        return f"{old}\n{line}" if old else line

    def snapshot(self, a, role):
        return {
            "role": role, "account_id": a["account_id"], "name": a["name"],
            "parent_name": a.get("parent_name") or "(no parent)",
            "address": f'{a["billing_street"]}, {a["billing_city"]}, {a["billing_state"]} {a["billing_zip"]}'.strip(", "),
            "care_type": a.get("care_type") or "", "status": a["status"], "phone": a.get("phone") or "",
            "lifetime_revenue": a.get("lifetime_revenue") or 0, "outstanding_ar": a.get("outstanding_ar") or 0,
            "contacts": [f'{c["name"]} ({c["title"]})' for c in self.contacts.get(a["account_id"], [])],
        }

    def new_account_fields(self, loc, note):
        types = N.care_types_from_site(loc.care_offerings)
        return {
            "name": loc.name, "parent_id": self.parent_id,
            "billing_street": loc.street, "billing_city": loc.city,
            "billing_state": loc.state, "billing_zip": loc.zip,
            "care_type": types[0] if types else "", "phone": loc.phone,
            "status": "Active", "note": note,
        }

    def survivor_rank(self, a, loc, target_parent):
        """Lower sorts first. Which copy of a duplicated facility do we keep?"""
        has_billing = (a.get("lifetime_revenue") or 0) > 0 or (a.get("outstanding_ar") or 0) > 0
        return (
            0 if has_billing else 1,                                   # 1. billing history lives here
            0 if a["status"] == "Active" else 1,                       # 2. still active
            -len(self.contacts.get(a["account_id"], [])),              # 3. most contacts
            0 if a.get("parent_id") == target_parent else 1,           # 4. already under the right parent
            0 if N.phone_digits(a.get("phone")) == N.phone_digits(loc.phone) else 1,  # 5. phone matches site
            -N.name_similarity(a["name"], loc.name),                   # 6. closest name
            a["account_id"],                                           # 7. stable tie-break
        )

    RANK_REASONS = ["it holds the billing history", "it is the active record", "it has the most contacts",
                    "it is already under the Bellhaven parent", "its phone matches the website",
                    "its name is closest to the website's", "no other difference, so the lowest account ID wins"]

    def why_kept(self, keep, dups, loc):
        """Name the first rule that separated the kept record from the runner-up."""
        ranks = sorted([keep] + dups, key=lambda a: self.survivor_rank(a, loc, self.parent_id))
        a, b = (self.survivor_rank(x, loc, self.parent_id) for x in ranks[:2])
        i = next((i for i in range(len(a)) if a[i] != b[i]), len(a) - 1)
        return (f'Kept {keep["account_id"]} ("{keep["name"]}") as the surviving record because {self.RANK_REASONS[i]} '
                f'(tie-break order: billing history, active, contacts, parent, phone, name, ID).')

    # ------------------------------------------------------------------ matching
    def find_matches(self, loc):
        """hits = accounts that are this facility; near = look-alikes we deliberately did not link."""
        lkey, lzip = N.street_key(loc.street), N.zip5(loc.zip)
        lcity, lstate, lcore = N.norm_city(loc.city), N.norm_state(loc.state), N.name_core(loc.name)
        hits, near = [], []
        for a in self.live:
            akey = N.street_key(a["billing_street"])
            acore = N.name_core(a["name"])
            same_city = N.norm_city(a["billing_city"]) == lcity and N.norm_state(a["billing_state"]) == lstate
            same_zip = N.zip5(a["billing_zip"]) == lzip
            overlap = len(lcore & acore) / len(lcore | acore) if (lcore or acore) else 0
            if lkey and akey == lkey and (same_city or same_zip):
                hits.append((a, f"{a['name']}: same street address ({a['billing_street']} = {loc.street})"))
            elif same_city and not akey and lcore and acore == lcore:
                hits.append((a, f"{a['name']}: same name and city; CRM has no street to compare ('{a['billing_street']}')"))
            elif (same_city or same_zip) and overlap >= 0.5:
                near.append((a, "same city and a similar name, but a different street address"))
            elif lcore and overlap >= 0.5:
                near.append((a, f"similar name, but in {a['billing_city']}, {a['billing_state']}"))
        return hits, near

    # ------------------------------------------------------------------ planning
    def plan(self, locations):
        proposals, confident, warnings = [], [], []
        claimed = {}  # account_id -> slug, so one account can't be matched to two locations
        for loc in locations:
            hits, near = self.find_matches(loc)
            for a, _ in hits:
                if a["account_id"] in claimed:
                    warnings.append(f'{a["name"]} matched both {claimed[a["account_id"]]} and {loc.slug}')
                claimed[a["account_id"]] = loc.slug
            p = self.plan_location(loc, hits, near)
            if p is None:
                a = hits[0][0]
                confident.append({"location": loc.to_dict(), "account": self.snapshot(a, "match"),
                                  "reasons": [r for _, r in hits]})
            else:
                proposals.append(p)
        proposals.extend(self.plan_not_on_website(set(claimed)))
        return proposals, confident, warnings

    def plan_location(self, loc, hits, near):
        reasons = [r for _, r in hits]
        near_notes = [f'Also checked {a["name"]} ({a["billing_street"]}, {a["billing_city"]}, {a["billing_state"]}): '
                      f'{why}; not linked.' for a, why in near]

        if not hits:
            fields = self.new_account_fields(loc, self.note(None, f"Created from Bellhaven website listing {loc.url}."))
            actions = [{"op": "create", "ref": NEW_REF, "fields": fields}]
            return self._proposal("new_location", loc.slug, f"{loc.name} ({loc.city}, {loc.state})", loc,
                                  [], actions, ["new account"],
                                  ["No CRM account at this street address or under this name in this city."] + near_notes,
                                  "medium" if near else "high")

        accts = [a for a, _ in hits]
        keep = min(accts, key=lambda a: self.survivor_rank(a, loc, self.parent_id))
        dups = [a for a in accts if a is not keep]
        actions, labels, snaps = [], [], [self.snapshot(keep, "keep")]
        confidence = "high" if all("same street address" in r for r in reasons) else "medium"

        # --- what's wrong with the record we keep
        set_, expect = {}, {}

        def change(field, new):
            set_[field] = new
            expect[field] = keep.get(field) or ""

        if keep["name"] != loc.name:
            change("name", loc.name)
            labels.append("name format" if N.same_name_ignoring_format(keep["name"], loc.name) else "rename")
            reasons.append(f'CRM name "{keep["name"]}" differs from the website name "{loc.name}".')
        if N.zip5(keep["billing_zip"]) != N.zip5(loc.zip):
            change("billing_zip", loc.zip)
            labels.append("ZIP fix")
            reasons.append(f'CRM ZIP {keep["billing_zip"]} differs from the website ZIP {loc.zip}.')
        if N.norm_city(keep["billing_city"]) != N.norm_city(loc.city):
            change("billing_city", loc.city)
            labels.append("city fix")
        if N.norm_state(keep["billing_state"]) != N.norm_state(loc.state):
            change("billing_state", loc.state)
            labels.append("state fix")
        if N.is_po_box(keep["billing_street"]):
            reasons.append(f'CRM street is a PO Box ({keep["billing_street"]}); kept as the billing address. '
                           f'Physical address per website: {loc.street}.')
        if keep["status"] != "Active":
            change("status", "Active")
            labels.append("reactivate")
            reasons.append(f'Listed on the website but CRM status is {keep["status"]}.')
        site_types = N.care_types_from_site(loc.care_offerings)
        if site_types and keep.get("care_type") not in site_types:
            change("care_type", site_types[0])
            labels.append("care type")
            reasons.append(f'Website offers {", ".join(loc.care_offerings)}; CRM care type is "{keep.get("care_type")}".')
        phone_note = []
        if N.phone_digits(keep.get("phone")) == N.phone_digits(loc.phone):
            phone_note.append("Phone number matches the website.")
        elif loc.phone:
            phone_note.append(f'Phone differs (CRM {keep.get("phone")}, website {loc.phone}); not changed, '
                              f'since a rep may use a direct line.')

        kind = "fix"
        survivor_ref = keep["account_id"]
        if keep.get("parent_id") != self.parent_id:
            current_parent = keep.get("parent_name") or "(none)"
            if sop_blocks_reparent(keep):
                # Billing SOP: leave the old account exactly as it is; create a new one; link it.
                kind, survivor_ref = "chow", NEW_REF
                set_.clear(); expect.clear()
                labels = ["CHOW: new account"]
                snaps[0]["role"] = "old account (kept for billing)"
                reasons.append(f'Belongs under Bellhaven but CRM parent is {current_parent}. It has lifetime revenue '
                               f'${keep["lifetime_revenue"]:,} and outstanding AR ${keep["outstanding_ar"]:,}, so per the '
                               f'billing SOP the old account stays exactly as it is.')
                note = self.note(None, f'Change of ownership: replaces account {keep["account_id"]} ("{keep["name"]}", '
                                       f'parent {current_parent}), which is kept for billing. Source: {loc.url}')
                actions.append({"op": "create", "ref": NEW_REF, "fields": self.new_account_fields(loc, note)})
                actions.append({"op": "patch", "account_id": keep["account_id"],
                                "set": {"chow_current_account": NEW_REF}, "expect": {"chow_current_account": ""}})
            else:
                change("parent_id", self.parent_id)
                labels.insert(0, "re-parent")
                billing = (f'revenue ${keep["lifetime_revenue"]:,} but AR ${keep["outstanding_ar"]:,}'
                           if keep["lifetime_revenue"] else "no revenue history")
                reasons.append(f'Belongs under Bellhaven but CRM parent is {current_parent}; {billing}, '
                               f'so the SOP allows moving this account directly.')

        if set_:
            actions.append({"op": "patch", "account_id": keep["account_id"], "set": dict(set_), "expect": dict(expect)})

        # --- the extra copies
        for d in dups:
            snaps.append(self.snapshot(d, "duplicate"))
            target = "the new account" if survivor_ref == NEW_REF else f'{keep["account_id"]} ("{loc.name}")'
            moving = self.contacts.get(d["account_id"], [])
            text = (f'Duplicate of {target}: same street address as the Bellhaven website listing {loc.url}. '
                    f'Marked Inactive.'
                    + (f' Active contacts moved to the surviving account: {", ".join(c["name"] for c in moving)}.' if moving else ''))
            actions.append({"op": "patch", "account_id": d["account_id"],
                            "set": {"duplicate_of_account": survivor_ref, "status": "Inactive", "note": self.note(d, text)},
                            "expect": {"duplicate_of_account": d.get("duplicate_of_account") or "",
                                       "status": d["status"], "note": d.get("note") or ""}})
            for c in self.contacts.get(d["account_id"], []):
                actions.append({"op": "move_contact", "contact_id": c["contact_id"], "contact_name": c["name"],
                                "from": d["account_id"], "to": survivor_ref})
            if sop_blocks_reparent(d):
                confidence = "medium"
                reasons.append(f'{d["name"]} has billing history; it is kept (never deleted) and points to the survivor.')
        if dups:
            kind = "chow" if kind == "chow" else "duplicate"
            labels.insert(0, f"{len(dups)} duplicate{'s' if len(dups) > 1 else ''}")
            reasons.append(self.why_kept(keep, dups, loc))

        if not actions:
            return None
        return self._proposal(kind, loc.slug, f"{loc.name} ({loc.city}, {loc.state})", loc, snaps, actions,
                              labels, reasons + phone_note + near_notes, confidence)

    def plan_not_on_website(self, matched_ids):
        out = []
        for a in self.live:
            if a.get("parent_id") != self.parent_id or a["account_id"] in matched_ids or a["status"] == "Inactive":
                continue
            akey, azip = N.street_key(a["billing_street"]), N.zip5(a["billing_zip"])
            twins = [b for b in self.live if b is not a and akey and N.street_key(b["billing_street"]) == akey
                     and N.zip5(b["billing_zip"]) == azip and b.get("parent_id") not in ("", self.parent_id)]
            title = f'{a["name"]} ({a["billing_city"]}, {a["billing_state"]})'
            snaps = [self.snapshot(a, "under Bellhaven, not on website")] + [self.snapshot(b, "same address, other parent") for b in twins]
            reasons = [f"Listed under the Bellhaven parent in the CRM, but not on the Bellhaven website."]
            if len(twins) == 1 and sop_blocks_reparent(a):
                t = twins[0]
                reasons += [f'{t["name"]} ({t["account_id"]}) is at the same street address under {t["parent_name"]}: '
                            f'the facility appears to have changed owners.',
                            f'Old account has revenue ${a["lifetime_revenue"]:,} and AR ${a["outstanding_ar"]:,}, so per the SOP it '
                            f'stays exactly as it is; the new owner already has an account, so we link to it instead of '
                            f'creating a second one.']
                actions = [{"op": "patch", "account_id": a["account_id"],
                            "set": {"chow_current_account": t["account_id"]}, "expect": {"chow_current_account": ""}}]
                out.append(self._proposal("chow", a["account_id"], title, None, snaps, actions,
                                          ["CHOW: link to new owner's account"], reasons, "medium"))
                continue
            if a["status"] == "Needs Review" and NOTE_TAG in (a.get("note") or ""):
                continue  # already flagged on an earlier run
            if twins:
                reasons.append("Accounts at the same address under another parent: "
                               + ", ".join(f'{b["name"]} ({b["parent_name"]})' for b in twins) + ".")
            text = ("Not listed on the Bellhaven website (checked every community page). "
                    "Needs review: closed, sold, or renamed?"
                    + (" Possible new owner record: " + ", ".join(b["account_id"] for b in twins) if twins else ""))
            actions = [{"op": "patch", "account_id": a["account_id"],
                        "set": {"status": "Needs Review", "note": self.note(a, text)},
                        "expect": {"status": a["status"], "note": a.get("note") or ""}}]
            out.append(self._proposal("not_on_website", a["account_id"], title, None, snaps, actions,
                                      ["flag: Needs Review"], reasons, "high"))
        return out

    def _proposal(self, kind, subject, title, loc, snaps, actions, labels, reasons, confidence):
        return {
            "fingerprint": fingerprint(kind, subject, actions),
            "kind": kind, "subject": subject, "title": title, "confidence": confidence,
            "labels": labels, "reasons": reasons,
            "location": loc.to_dict() if loc else None,
            "accounts": snaps, "actions": actions,
        }
