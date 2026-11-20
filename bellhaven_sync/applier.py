"""Write one approved proposal to the CRM.

Safety checks, in order:
  1. Preflight (before any write): every field we planned to change must still hold
     the value we saw when the proposal was made. If someone edited the account since,
     we stop and mark the proposal stale instead of overwriting their change.
  2. SOP guard: re-read revenue and AR right before any parent change; refuse if the
     billing SOP says the account must be preserved.
  3. Idempotent creates: before creating an account, look for one we already created
     for the same facility (e.g. a half-finished earlier attempt) and reuse it.
  4. Read-back: after writing, re-read every patched account and confirm the values.
"""
from . import normalize as N
from .matcher import NEW_REF, sop_blocks_reparent


class StaleProposal(RuntimeError):
    pass


def _resolve(value, refs):
    return refs[value] if isinstance(value, str) and value in refs else value


def _existing_created(client, fields):
    for a in client.list_accounts(zip=fields["billing_zip"]):
        if (a.get("created_by_candidate") and a["name"] == fields["name"]
                and a.get("parent_id") == fields["parent_id"]
                and N.street_key(a["billing_street"]) == N.street_key(fields["billing_street"])):
            return a["account_id"]
    return None


def preflight(client, actions):
    problems = []
    for act in actions:
        if act["op"] == "patch":
            cur = client.get_account(act["account_id"])
            for field, old in act["expect"].items():
                if (cur.get(field) or "") != (old or ""):
                    problems.append(f'{act["account_id"]}.{field} is now {cur.get(field)!r}, expected {old!r}')
            if "parent_id" in act["set"] and sop_blocks_reparent(cur):
                problems.append(f'{act["account_id"]} now has revenue AND open AR: billing SOP forbids a re-parent')
        elif act["op"] == "move_contact":
            c = client._get(f'/contacts/{act["contact_id"]}')
            if c["account_id"] != act["from"]:
                problems.append(f'contact {act["contact_id"]} moved since the proposal was made')
    if problems:
        raise StaleProposal("; ".join(problems))


def apply(client, proposal, log=lambda op, target, detail: None):
    actions = proposal["actions"]
    preflight(client, actions)
    refs, done = {}, []
    for act in actions:
        if act["op"] == "create":
            existing = _existing_created(client, act["fields"])
            new_id = existing or client.create_account(act["fields"])
            refs[act["ref"]] = new_id
            msg = f"reused {new_id} (already created earlier)" if existing else f"created {new_id}"
            done.append(msg); log("create", new_id, act["fields"])
        elif act["op"] == "patch":
            changes = {k: _resolve(v, refs) for k, v in act["set"].items()}
            client.update_account(act["account_id"], changes)
            done.append(f'updated {act["account_id"]}: {", ".join(changes)}')
            log("patch", act["account_id"], {"before": act["expect"], "after": changes})
        elif act["op"] == "move_contact":
            to = _resolve(act["to"], refs)
            client.update_contact(act["contact_id"], {"account_id": to})
            done.append(f'moved contact {act["contact_name"]} to {to}')
            log("move_contact", act["contact_id"], {"from": act["from"], "to": to})

    # Read-back check
    for act in actions:
        if act["op"] == "patch":
            cur = client.get_account(act["account_id"])
            for k, v in act["set"].items():
                want = _resolve(v, refs)
                if (cur.get(k) or "") != (want or ""):
                    raise RuntimeError(f'Read-back failed: {act["account_id"]}.{k} = {cur.get(k)!r}, wanted {want!r}')
    done.append("read-back verified")
    return {"steps": done, "created": refs.get(NEW_REF)}
