"""Tests run against an in-memory fake CRM, never the real sandbox.

    python -m unittest discover -s tests -v
"""
import copy
import tempfile
import unittest
from pathlib import Path

from bellhaven_sync import normalize as N
from bellhaven_sync.applier import StaleProposal, apply
from bellhaven_sync.crm import CRMClient, ReadOnlyError
from bellhaven_sync.matcher import Matcher
from bellhaven_sync.scraper import Location, parse_detail
from bellhaven_sync.store import Store
import run_pipeline

PARENT = "Bellhaven Senior Living (Parent Account)"


def acct(aid, name, parent="P1", street="", city="", state="", zip_="", rev=0, ar=0, status="Active", phone="", care="Skilled Nursing"):
    return {"account_id": aid, "name": name, "parent_id": parent, "parent_name": "", "billing_street": street,
            "billing_city": city, "billing_state": state, "billing_zip": zip_, "care_type": care, "status": status,
            "phone": phone, "lifetime_revenue": rev, "outstanding_ar": ar, "chow_current_account": "",
            "duplicate_of_account": "", "note": "", "created_by_candidate": False}


def loc(slug, name, street, city, state, zip_, care=("Short-Term Rehabilitation & Nursing",), phone=""):
    return Location(slug=slug, url=f"https://site/communities/{slug}", name=name, street=street, city=city,
                    state=state, zip=zip_, care_offerings=list(care), phone=phone)


def world():
    accounts = [
        acct("P1", PARENT, parent=""), acct("P2", "Cedar Trail Communities (Parent Account)", parent=""),
        acct("P3", "Millstone Health Partners (Parent Account)", parent=""),
        acct("A1", "Bellhaven of Marion", street="199 Barks Road West", city="Marion", state="OH", zip_="43302"),
        acct("A2", "Bellhaven of New Carlisle", street="875 Elm St", city="New Carlisle", state="OH", zip_="45344"),
        acct("A3", "Bellhaven of Tiffin", parent="P2", street="45 St Lawrence Dr", city="Tiffin", state="OH", zip_="44883", rev=84000, ar=12400),
        acct("A4", "Bellhaven Crossings of Lima", parent="P2", street="3115 N Cole St", city="Lima", state="OH", zip_="45801", rev=47000, ar=0),
        acct("A5", "Bellhaven of Owosso", street="1120 W Main St", city="Owosso", state="MI", zip_="48867", phone="(734) 503-1363"),
        acct("A6", "Bellhaven of Owosso", street="1120 West Main Street", city="Owosso", state="MI", zip_="48867"),
        acct("A7", "Bellhaven of Coldwater", street="90 N Michigan Ave", city="Coldwater", state="MI", zip_="49036"),
        acct("A8", "Bellhaven of Sandusky", street="2715 Columbus Ave", city="Sandusky", state="OH", zip_="44870", rev=130000, ar=5200),
        acct("A9", "Millstone Care of Sandusky", parent="P3", street="2715 Columbus Ave", city="Sandusky", state="OH", zip_="44870"),
        acct("A10", "Bellhaven of Ashtabula", street="PO Box 517", city="Ashtabula", state="OH", zip_="44004"),
    ]
    # Like the real data: one contact on each Owosso copy, so the phone match decides which copy survives.
    contacts = [{"contact_id": "C1", "account_id": "A6", "name": "Tricia Lindqvist", "title": "Admissions Director", "is_active": True},
                {"contact_id": "C0", "account_id": "A5", "name": "Gloria Lambert", "title": "Administrator", "is_active": True}]
    locations = [
        loc("marion", "Bellhaven of Marion", "199 Barks Rd W", "Marion", "OH", "43302"),
        loc("new-carlisle", "Bellhaven of New Carlisle", "875 Elm Street", "New Carlisle", "OH", "45344"),
        loc("carlisle", "Bellhaven of Carlisle", "640 Walnut Bottom Rd", "Carlisle", "PA", "17015"),
        loc("tiffin", "Bellhaven of Tiffin", "45 St Lawrence Dr", "Tiffin", "OH", "44883"),
        loc("lima", "Bellhaven Crossings of Lima", "3115 N Cole St", "Lima", "OH", "45801"),
        loc("owosso", "Bellhaven of Owosso", "1120 W Main St", "Owosso", "MI", "48867", ("Assisted Living",), "(734) 503-1363"),
        loc("ashtabula", "Bellhaven of Ashtabula", "3156 W Prospect Rd", "Ashtabula", "OH", "44004"),
    ]
    for a in accounts:
        a["care_type"] = "Assisted Living" if "Owosso" in a["name"] else a["care_type"]
    return accounts, contacts, locations


class FakeCRM:
    """Implements the same methods as CRMClient, in memory."""
    def __init__(self, accounts, contacts, read_only=False):
        self.accounts = {a["account_id"]: copy.deepcopy(a) for a in accounts}
        self.contacts = {c["contact_id"]: copy.deepcopy(c) for c in contacts}
        self.read_only, self.n = read_only, 0

    def list_accounts(self, zip=None, **_):
        return [copy.deepcopy(a) for a in self.accounts.values() if zip is None or a["billing_zip"] == zip]

    def list_contacts(self, **_):
        return [copy.deepcopy(c) for c in self.contacts.values()]

    def get_account(self, aid):
        return copy.deepcopy(self.accounts[aid])

    def _get(self, path):
        return copy.deepcopy(self.contacts[path.rsplit("/", 1)[1]])

    def create_account(self, fields):
        self.n += 1
        aid = f"NEW{self.n}"
        self.accounts[aid] = {**acct(aid, ""), **fields, "created_by_candidate": True}
        return aid

    def update_account(self, aid, changes):
        self.accounts[aid].update(changes)

    def update_contact(self, cid, changes):
        self.contacts[cid].update(changes)


class NormalizeTests(unittest.TestCase):
    def test_street_variants_match(self):
        self.assertEqual(N.street_key("199 Barks Rd W"), N.street_key("199 Barks Road West"))
        self.assertEqual(N.street_key("1250 NW Franklin Street"), N.street_key("1250 Northwest Franklin St"))
        self.assertEqual(N.street_key("3313 Wilmington Pk"), N.street_key("3313 Wilmington Pike"))

    def test_po_box_is_not_a_street(self):
        self.assertEqual(N.street_key("PO Box 517"), ())

    def test_name_format_vs_rename(self):
        self.assertTrue(N.same_name_ignoring_format("Bellhaven Rehab and Nursing of Grove City",
                                                    "Bellhaven Rehabilitation & Nursing of Grove City"))
        self.assertFalse(N.same_name_ignoring_format("Riverbend Manor Care Center", "Bellhaven of Chagrin Falls"))


class MatcherTests(unittest.TestCase):
    def setUp(self):
        accounts, contacts, self.locations = world()
        self.m = Matcher(accounts, contacts, PARENT, today="2026-11-20")
        self.props, self.confident, _ = self.m.plan(self.locations)
        self.by = {p["subject"]: p for p in self.props}

    def test_confident_match_needs_no_proposal(self):
        self.assertNotIn("marion", self.by)
        self.assertIn("marion", [c["location"]["slug"] for c in self.confident])

    def test_carlisle_pa_is_not_new_carlisle_oh(self):
        self.assertEqual(self.by["carlisle"]["kind"], "new_location")
        self.assertNotIn("new-carlisle", self.by)

    def test_chow_when_revenue_and_open_ar(self):
        p = self.by["tiffin"]
        self.assertEqual(p["kind"], "chow")
        self.assertEqual([a["op"] for a in p["actions"]], ["create", "patch"])
        self.assertEqual(p["actions"][1]["set"], {"chow_current_account": "$new"})  # old account otherwise untouched

    def test_reparent_when_revenue_but_no_ar(self):
        p = self.by["lima"]
        self.assertEqual(p["kind"], "fix")
        self.assertEqual(p["actions"][0]["set"], {"parent_id": "P1"})

    def test_duplicates_keep_one_and_move_contacts(self):
        p = self.by["owosso"]
        self.assertEqual(p["kind"], "duplicate")
        dup = [a for a in p["actions"] if a["op"] == "patch"][0]
        self.assertEqual(dup["account_id"], "A6")
        self.assertEqual(dup["set"]["duplicate_of_account"], "A5")
        self.assertEqual(dup["set"]["status"], "Inactive")
        self.assertIn({"op": "move_contact", "contact_id": "C1", "contact_name": "Tricia Lindqvist", "from": "A6", "to": "A5"}, p["actions"])

    def test_po_box_matches_on_name_and_city(self):
        self.assertIn("ashtabula", [c["location"]["slug"] for c in self.confident])

    def test_not_on_website(self):
        self.assertEqual(self.by["A7"]["actions"][0]["set"]["status"], "Needs Review")

    def test_divested_with_billing_links_to_new_owner(self):
        p = self.by["A8"]
        self.assertEqual(p["kind"], "chow")
        self.assertEqual(p["actions"], [{"op": "patch", "account_id": "A8", "set": {"chow_current_account": "A9"},
                                         "expect": {"chow_current_account": ""}}])


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self.accounts, self.contacts, self.locations = world()
        self.crm = FakeCRM(self.accounts, self.contacts)
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "state.db")

    def tearDown(self):
        self.store.db.close()
        self.tmp.cleanup()

    def run_once(self):
        return run_pipeline.run(store=self.store, client=self.crm, scraper=lambda: (self.locations, []),
                                today="2026-11-20", quiet=True, report_dir=Path(self.tmp.name))[0]

    def test_rerun_is_safe(self):
        first = self.run_once()
        self.assertGreater(first["queue"]["new"], 0)
        again = self.run_once()  # nothing decided yet: same proposals, not duplicated
        self.assertEqual(again["queue"]["new"], 0)
        self.assertEqual(again["queue"]["refreshed"], first["queue"]["new"])

        pending = self.store.proposals(["pending"])
        rejected = pending[0]
        self.store.decide(rejected["id"], "rejected", "test", "disagree")
        for row in pending[1:]:
            apply(self.crm, row["payload"])
            self.store.decide(row["id"], "applied", "test", "", {})

        third = self.run_once()
        self.assertEqual(third["queue"]["new"], 0, "approved changes must not be re-proposed")
        self.assertEqual(third["queue"]["skipped_already_decided"], 1, "the rejected one must stay rejected")
        self.assertEqual(third["proposals_this_run"], 1, "only the rejected proposal is still produced")
        self.assertEqual(self.store.counts().get("pending", 0), 0)

    def test_create_is_not_repeated(self):
        self.run_once()
        tiffin = [r for r in self.store.proposals(["pending"]) if r["payload"]["subject"] == "tiffin"][0]
        apply(self.crm, tiffin["payload"])
        created = [a for a in self.crm.accounts.values() if a["created_by_candidate"]]
        self.assertEqual(len(created), 1)
        self.assertEqual(self.crm.accounts["A3"]["chow_current_account"], created[0]["account_id"])
        self.assertEqual(self.crm.accounts["A3"]["parent_id"], "P2", "old CHOW account keeps its parent")

    def test_stale_proposal_is_refused(self):
        self.run_once()
        lima = [r for r in self.store.proposals(["pending"]) if r["payload"]["subject"] == "lima"][0]
        self.crm.accounts["A4"]["outstanding_ar"] = 500  # AR appears after the proposal was made
        with self.assertRaises(StaleProposal):
            apply(self.crm, lima["payload"])
        self.assertEqual(self.crm.accounts["A4"]["parent_id"], "P2")

    def test_pipeline_client_cannot_write(self):
        with self.assertRaises(ReadOnlyError):
            CRMClient(token="x", read_only=True).update_account("A1", {"name": "y"})


class ScraperTests(unittest.TestCase):
    def test_parse_detail(self):
        html = ('<h1>Bellhaven Shores of Erie</h1><dl class="detail"><dt>Address</dt><dd>4930 W Lake Rd<br>Erie, PA 16505</dd>'
                '<dt>Care Offerings</dt><dd><span class="badge">Assisted Living</span><span class="badge">Memory Support</span></dd>'
                '<dt>Administrator</dt><dd>Angela Quinn</dd><dt>Phone</dt><dd>(614) 792-9343</dd></dl>')
        l = parse_detail("bellhaven-shores-of-erie", html)
        self.assertEqual((l.name, l.street, l.city, l.state, l.zip), ("Bellhaven Shores of Erie", "4930 W Lake Rd", "Erie", "PA", "16505"))
        self.assertEqual(l.care_offerings, ["Assisted Living", "Memory Support"])
        self.assertEqual(l.phone, "(614) 792-9343")


if __name__ == "__main__":
    unittest.main()
