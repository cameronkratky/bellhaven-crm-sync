"""Daily job: scrape the website, read the CRM, propose changes for review.

    python run_pipeline.py            # scrape + match + queue proposals (never writes to the CRM)
    python run_pipeline.py --dry-run  # same, but don't save anything; just print

Writes happen only when a reviewer approves a proposal in review_app.py.
"""
import argparse
import json
import sys
from datetime import date

from bellhaven_sync import config
from bellhaven_sync.crm import CRMClient
from bellhaven_sync.matcher import Matcher
from bellhaven_sync.scraper import scrape
from bellhaven_sync.store import Store


def run(dry_run=False, store=None, client=None, scraper=scrape, today=None, quiet=False):
    say = (lambda *a: None) if quiet else print
    if not config.TOKEN and client is None:
        sys.exit("Set BELLHAVEN_TOKEN (see .env.example).")
    client = client or CRMClient(read_only=True)  # the pipeline itself can never write

    locations, site_notes = scraper()
    accounts = client.list_accounts()
    contacts = client.list_contacts()
    matcher = Matcher(accounts, contacts, config.PARENT_NAME, today=today)
    proposals, confident, warnings = matcher.plan(locations)

    by_kind = {}
    for p in proposals:
        by_kind[p["kind"]] = by_kind.get(p["kind"], 0) + 1
    summary = {
        "date": today or date.today().isoformat(),
        "website_locations": len(locations), "crm_accounts": len(accounts),
        "confident_matches_no_change": len(confident),
        "proposals_this_run": len(proposals), "by_kind": by_kind,
        "site_notes": site_notes, "warnings": warnings,
    }
    if not dry_run:
        store = store or Store(config.DB_PATH)
        run_id = store.start_run()
        summary["queue"] = store.save_proposals(run_id, proposals)
        store.finish_run(run_id, summary)
        config.REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (config.REPORT_DIR / "latest.json").write_text(
            json.dumps({"summary": summary, "confident": confident, "proposals": proposals}, indent=1), encoding="utf-8")

    say(json.dumps(summary, indent=1))
    if dry_run:
        for p in proposals:
            say(f'\n[{p["kind"]}] {p["title"]}  ({", ".join(p["labels"])}; {p["confidence"]})')
            for r in p["reasons"]:
                say("   -", r)
            for a in p["actions"]:
                say("   >", json.dumps({k: v for k, v in a.items() if k not in ("expect",)})[:300])
    return summary, proposals, confident


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    run(dry_run=ap.parse_args().dry_run)
