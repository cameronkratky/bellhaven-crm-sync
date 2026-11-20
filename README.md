# Bellhaven → CRM sync

Keeps the CRM's record of which facilities Bellhaven Senior Living owns in line with Bellhaven's own website.
It runs daily, proposes changes with evidence, and writes to the CRM only when a reviewer approves.

```
website (35 communities) ──scrape──┐
                                   ├── match + classify ──> proposals (SQLite) ──> review app ──approve──> CRM API
CRM (accounts + contacts) ──read───┘        never writes                         reject = never re-proposed
```

## Run it

```bash
pip install -r requirements.txt          # only dependency: requests
cp .env.example .env                     # paste your token into .env
python run_pipeline.py --dry-run         # print proposals, save nothing
python run_pipeline.py                   # queue proposals for review (still writes nothing to the CRM)
python review_app.py                     # http://127.0.0.1:8000 : approve / reject
python -m unittest discover -s tests -t .   # 16 tests, run against an in-memory fake CRM
python verify_crm.py                     # read-only check of the CRM's end state vs. the website
```

Demo (3 min): https://www.loom.com/share/00e042b6d9d54393b8284db0460409d4

## Results (my CRM copy, Nov 20)

| | |
|---|---|
| Website locations | 35 (the directory lists 34; Findlay is linked only from the home page) |
| Already correct | 13 |
| Proposals | 25: **21 approved, 4 rejected** |
| Accounts created | 6: 4 new locations (Batavia, Union Square, Carlisle PA, Amberly Manor in Hudson) + 2 CHOW replacements (Tiffin, Marietta) |
| CHOW links | 3: Tiffin and Marietta (new Bellhaven accounts), Sandusky (to Millstone's existing account at the same address) |
| Duplicates retired | 7 (Port Clinton, Erie, Kettering x2, Monroe x2, Owosso), one contact moved to the survivor |
| Re-parented | 4 (Lima, Findlay, Zanesville, Kettering); renamed: Chagrin Falls, Willow Creek, Chesterton, Zanesville, Kettering; ZIP fixed: Portsmouth |
| Flagged Needs Review | 2 (Alliance, Coldwater: under Bellhaven, not on the website) |
| Rejected | 4 formatting-only name changes (The Arbors, Grove City, Sycamore Ridge, Ashland) |
| Re-run after review | 0 new proposals; the 4 rejected ones skipped |
| `verify_crm.py` | 4/4 PASS: every website location maps to one Active Bellhaven account; duplicates Inactive with a valid survivor; CHOW links valid; nothing under Bellhaven missing from the site without a flag |

Daily schedule: `schedule/crontab.txt` (cron) or `.github/workflows/daily-sync.yml` (GitHub Actions).

## Files

| File | What it does |
|---|---|
| `bellhaven_sync/scraper.py` | Reads the paginated directory **and** every `/communities/` link on the home and About pages. The directory lists 34; the home page announces a 35th (Findlay) that the directory leaves out. |
| `bellhaven_sync/normalize.py` | Address and name keys: `199 Barks Rd W` = `199 Barks Road West`, `Wilmington Pk` = `Wilmington Pike`, PO Boxes recognized. |
| `bellhaven_sync/matcher.py` | Matching, classification, the billing SOP, and every proposal's evidence. |
| `bellhaven_sync/store.py` | SQLite state: proposals, decisions, audit log. Makes re-runs safe. |
| `bellhaven_sync/applier.py` | Writes one approved proposal: preflight check, SOP re-check, idempotent creates, read-back. |
| `bellhaven_sync/crm.py` | API client. Read-only unless created with `read_only=False`, which only the Approve button does. |
| `review_app.py` | Local review UI (standard library only). |

## Matching approach

1. **Street address first.** Each website location is compared with every live CRM account on a normalized key (house number + street words, suffixes and directions standardized, word order ignored), plus the same city/state or ZIP. A building's address is the most stable identifier: names change when facilities are sold or rebranded; addresses don't.
2. **Name + city only when the CRM has no street to compare** (for example a PO Box billing address). Same city and a different street address means a different building, even with the same name.
3. **Look-alikes are shown, never linked.** "Bellhaven of Carlisle, PA" vs "Bellhaven of New Carlisle, OH"; "Amberly Manor" in Hudson, OH vs one in Colorado Springs; "Bellhaven at Union Square" (118 Union Square Dr) vs "Union Square Senior Living" (240 Market St). Each appears in the evidence as "also checked … not linked" so the reviewer can overrule.
4. **Then classify:**

| Outcome | Rule | What the proposal does |
|---|---|---|
| Confident match | one account, nothing differs | no proposal (listed under "Confident matches") |
| Match needs a fix | wrong parent, outdated name, wrong ZIP/city/state, not Active, care type not offered | PATCH only the fields that differ |
| Change of ownership (CHOW) | needs a new parent **and** lifetime revenue > 0 **and** outstanding AR > 0 | old account left exactly as is; new account created under Bellhaven; `chow_current_account` on the old one points to it |
| Duplicates | 2+ live accounts at the same street address | keep one; others get `duplicate_of_account` + Inactive + a note; their active contacts move to the survivor |
| New location | no account matches | create it under the Bellhaven parent |
| No longer on website | under Bellhaven in the CRM, not on the site | Needs Review + note. If another owner already has an account at the same address **and** the SOP applies, link it with `chow_current_account` instead of creating a second one (Sandusky → Millstone) |

**Which duplicate survives:** billing history → active → most contacts → already under Bellhaven → phone matches website → closest name → lowest ID. The proposal names the rule that decided it.

**Decisions I made on purpose:**
- Real renames are synced to the website's name ("Riverbend Manor Care Center" → "Bellhaven of Chagrin Falls"). Formatting-only differences ("Rehab and" vs "Rehabilitation &", "of" vs "at") are still proposed but labeled "name format"; in review I rejected all four, because they add noise to the record without changing anything a rep would notice. Rejected means never proposed again.
- Phone numbers that differ are reported, not changed: a rep may be using a direct line.
- A PO Box billing address is kept (billing may mail invoices there); the physical address is shown in the evidence. This is Bellhaven of Ashtabula: the CRM has PO Box 517, and the website has 3156 W Prospect Rd. It matches on name + city + ZIP, and its phone matches the website.
- Accounts already marked `duplicate_of_account` or `chow_current_account` are treated as history and skipped, so a re-run after approval is quiet.
- The SOP is checked twice: when the proposal is built, and again from a fresh read just before writing.

## Re-run safety

Every proposal has a fingerprint: a hash of the facility plus the exact target values (notes excluded, since they carry dates).

- Approved or rejected → never proposed again.
- Still pending → refreshed in place, not duplicated.
- No longer produced (the website or CRM changed) → marked superseded, so nobody approves an outdated change.
- Approving re-reads every field first; if anyone changed it since the proposal was made, nothing is written and the proposal is marked stale.
- Creates look for an account this tool already made for the same facility before creating another.

Tested in `tests/test_pipeline.py`: run twice → 0 new; approve all but one and reject one → the third run produces only the rejected one, which is skipped.

## How I used AI tools

I used Claude Code (an AI coding agent) for this. It pulled the API docs, the website, and all 121 accounts so we could study the data before writing code; it wrote the code and tests. I set the requirements and made the judgment calls above, and I decided every proposal. I clicked six of them in the review app myself (the ones in the demo takes); the other 19 I decided from the reviewed list and had Claude Code submit through the same Approve/Reject endpoints, so each one still ran the preflight, SOP, and read-back checks. Every write to the CRM went through that path.

Checking the AI's work: 16 tests against a fake in-memory CRM; a dry run compared line by line against the raw data before anything was queued; a second, independent AI review of the proposals against the brief (it passed all 9 checks and caught 4 wording fixes); read-back of every updated account (created accounts and moved contacts are not re-read yet); and `verify_crm.py` on the final state.

One mistake to disclose: while probing which POST fields the API validates, two calls passed validation and created two empty accounts named "x" (`001EF3B8E82E0BF4FC`, `001B835924F6F6B17C`). The API has no delete, so I renamed both "TEST RECORD - API validation probe (not a facility)", set them Inactive, and wrote a note on each saying why. They account for 2 of the CRM's 129 accounts today (121 original + 2 probe records + 6 created by approved proposals). They have no address or parent, so they never match a location. After that, every write went through the review app, and the tests use an in-memory fake CRM instead of the real one.

## What I'd build next

- Contacts: add the website's administrator to new accounts, and flag contacts whose name differs from the website.
- Roll up acquired parents: Harborview Care Group (acquired 2025, per Bellhaven's About page) has no remaining live children once this queue is approved.
- Run the same pipeline for every multi-site operator, not only Bellhaven, with CMS's change-of-ownership file as a second source.
- Alerts when a daily run finds something new, plus a weekly summary of approved changes for sales leadership.
- A hosted database for decisions instead of a SQLite file. Today the review app and a GitHub Actions run would each keep their own `state.db`; one shared database fixes that.
