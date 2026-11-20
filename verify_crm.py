"""Read-only check of the CRM's end state against the website. Prints PASS/FAIL per check.

    python verify_crm.py
"""
import sys

from bellhaven_sync import config
from bellhaven_sync import normalize as N
from bellhaven_sync.crm import CRMClient
from bellhaven_sync.matcher import sop_blocks_reparent
from bellhaven_sync.scraper import scrape


def main():
    crm = CRMClient(read_only=True)
    accounts, contacts = crm.list_accounts(), crm.list_contacts()
    locations, _ = scrape()
    by_id = {a["account_id"]: a for a in accounts}
    parent = next(a for a in accounts if a["name"] == config.PARENT_NAME)["account_id"]
    live = [a for a in accounts if not a["duplicate_of_account"] and not a["chow_current_account"]
            and not a["name"].endswith("(Parent Account)")]
    results = []

    # 1. Every website location maps to exactly one live, Active account under the Bellhaven parent.
    problems = []
    for loc in locations:
        key = N.street_key(loc.street)
        hits = [a for a in live if N.zip5(a["billing_zip"]) == N.zip5(loc.zip) and (
            (key and N.street_key(a["billing_street"]) == key)
            or (not N.street_key(a["billing_street"]) and N.name_core(a["name"]) == N.name_core(loc.name)))]
        if len(hits) != 1:
            problems.append(f"{loc.name}: {len(hits)} live accounts")
        elif hits[0]["parent_id"] != parent or hits[0]["status"] != "Active":
            problems.append(f"{loc.name}: parent {hits[0]['parent_name']!r}, status {hits[0]['status']}")
    results.append((f"every website location -> one Active Bellhaven account ({len(locations)} checked)", problems))

    # 2. Duplicates are Inactive and point at an account that exists and is not itself a duplicate.
    dups = [a for a in accounts if a["duplicate_of_account"]]
    results.append((f"duplicates Inactive with a valid survivor ({len(dups)} checked)",
                    [a["name"] for a in dups if a["status"] != "Inactive"
                     or a["duplicate_of_account"] not in by_id
                     or by_id[a["duplicate_of_account"]]["duplicate_of_account"]]))

    # 3. CHOW: old accounts keep their billing history, point at an existing account under another parent.
    chow = [a for a in accounts if a["chow_current_account"]]
    results.append((f"CHOW links valid ({len(chow)} checked)",
                    [a["name"] for a in chow if a["chow_current_account"] not in by_id
                     or not sop_blocks_reparent(a)
                     or by_id[a["chow_current_account"]]["parent_id"] == a["parent_id"]]))

    # 4. Nothing under Bellhaven is missing from the website without being flagged or linked.
    site_ids = set()
    for loc in locations:
        key = N.street_key(loc.street)
        site_ids |= {a["account_id"] for a in live if key and N.street_key(a["billing_street"]) == key}
        site_ids |= {a["account_id"] for a in live if not N.street_key(a["billing_street"])
                     and N.name_core(a["name"]) == N.name_core(loc.name)}
    stray = [a["name"] for a in live if a["parent_id"] == parent and a["account_id"] not in site_ids
             and a["status"] == "Active"]
    results.append(("no unflagged Bellhaven account missing from the website", stray))

    failed = 0
    for name, probs in results:
        print(("PASS " if not probs else "FAIL ") + name + ("" if not probs else f": {probs}"))
        failed += bool(probs)
    print(f"{len(accounts)} accounts, {len(contacts)} contacts read.")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
