"""Local review app. Run:  python review_app.py   then open http://127.0.0.1:8000

Each pending proposal shows its evidence (website vs. every CRM record involved) and
exactly which API calls approving it will make. Approve -> writes to the CRM through
the API (with preflight + read-back checks). Reject -> recorded, never proposed again.
Nothing writes without a click on Approve.
"""
import getpass
import json
import os
import sys
import traceback
from html import escape
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse, quote

from bellhaven_sync import config
from bellhaven_sync.applier import StaleProposal, apply
from bellhaven_sync.crm import CRMClient
from bellhaven_sync.matcher import NEW_REF
from bellhaven_sync.store import Store
import run_pipeline

REVIEWER = os.environ.get("REVIEWER") or getpass.getuser()
store = Store(config.DB_PATH)

KIND_LABEL = {"chow": "Change of ownership", "duplicate": "Duplicates", "fix": "Match needs a fix",
              "new_location": "New location", "not_on_website": "No longer on website"}
KIND_ORDER = ["chow", "duplicate", "fix", "new_location", "not_on_website"]

CSS = """
*{box-sizing:border-box} body{margin:0;font:14px/1.45 -apple-system,Segoe UI,Roboto,sans-serif;color:#1d2433;background:#f4f5f7}
header{background:#13315c;color:#fff;padding:14px 24px;display:flex;gap:18px;align-items:center;flex-wrap:wrap}
header h1{font-size:17px;margin:0 12px 0 0;font-weight:600} header a{color:#cfe0ff;text-decoration:none}
header a.on{color:#fff;font-weight:600;border-bottom:2px solid #fff}
.wrap{max-width:1180px;margin:0 auto;padding:18px 24px 60px}
.stats{display:flex;gap:10px;flex-wrap:wrap;margin:6px 0 16px}
.stat{background:#fff;border:1px solid #dde1e8;border-radius:6px;padding:8px 14px} .stat b{font-size:18px;display:block}
.card{background:#fff;border:1px solid #dde1e8;border-radius:8px;margin:14px 0;padding:16px 18px}
.card h3{margin:0 0 6px;font-size:16px} .pill{display:inline-block;border-radius:10px;padding:1px 9px;font-size:12px;margin-right:6px;background:#e8edf5}
.k-chow{background:#fde8d8;color:#8a3b00}.k-duplicate{background:#efe3fb;color:#5b2a86}.k-fix{background:#e3f0ff;color:#0b4a8b}
.k-new_location{background:#e2f6e9;color:#17663a}.k-not_on_website{background:#fff4cc;color:#7a5b00}
.conf-medium{background:#fff4cc}.conf-high{background:#e2f6e9}
h4{margin:14px 0 4px;font-size:13px;text-transform:uppercase;letter-spacing:.04em;color:#5a6478}
ul{margin:4px 0;padding-left:20px} table{border-collapse:collapse;width:100%;font-size:13px;margin-top:4px}
th,td{border:1px solid #e3e6ec;padding:5px 8px;text-align:left;vertical-align:top} th{background:#f7f8fa;font-weight:600}
td.diff{background:#fff3e0} .act li{margin:3px 0} code{background:#f1f3f6;padding:0 4px;border-radius:3px}
.btns{display:flex;gap:10px;margin-top:12px;align-items:center;flex-wrap:wrap}
button{border:0;border-radius:6px;padding:8px 16px;font-weight:600;cursor:pointer}
.approve{background:#1d7a46;color:#fff}.reject{background:#b42318;color:#fff}.run{background:#fff;color:#13315c}
input[type=text]{padding:7px 9px;border:1px solid #c9ced8;border-radius:6px;width:300px}
.flash{padding:10px 14px;border-radius:6px;margin:10px 0;background:#e2f6e9;border:1px solid #9fd9b5}
.flash.err{background:#fde7e7;border-color:#f3a8a8} .muted{color:#6b7385} a{color:#0b4a8b}
"""


def crm_link(account_id, label=None):
    if not account_id or account_id == NEW_REF:
        return escape(label or "(new account)")
    return f'<a href="{escape(config.crm_browser_url(account_id))}" target="_blank">{escape(label or account_id)}</a>'


def describe_actions(p, result=None):
    names = {a["account_id"]: a["name"] for a in p["accounts"]}
    new_id = (result or {}).get("created") if isinstance(result, dict) else None
    def ref(v):
        if v == NEW_REF:
            return crm_link(new_id, new_id) if new_id else "<i>the new account</i>"
        return crm_link(v, f"{names[v]} ({v})" if v in names else v)
    out = []
    for a in p["actions"]:
        if a["op"] == "create":
            f = a["fields"]
            out.append(f'<b>Create</b> account “{escape(f["name"])}” under Bellhaven Senior Living: '
                       f'{escape(f["billing_street"])}, {escape(f["billing_city"])}, {escape(f["billing_state"])} {escape(f["billing_zip"])}'
                       f' · {escape(f["care_type"])} · {escape(f["status"])}')
        elif a["op"] == "patch":
            parts = []
            for k, v in a["set"].items():
                old = a["expect"].get(k, "")
                if k == "note":
                    added = v[len(old):].strip() if v.startswith(old) else v
                    parts.append(f'<code>note</code> += “{escape(added)}”')
                elif k in ("parent_id", "duplicate_of_account", "chow_current_account"):
                    parts.append(f'<code>{k}</code>: {escape(old) or "∅"} → {ref(v)}')
                else:
                    parts.append(f'<code>{k}</code>: “{escape(str(old))}” → “{escape(str(v))}”')
            out.append(f'<b>Update</b> {ref(a["account_id"])}: ' + "; ".join(parts))
        elif a["op"] == "move_contact":
            out.append(f'<b>Move contact</b> {escape(a["contact_name"])} from {ref(a["from"])} to {ref(a["to"])}')
    return "".join(f"<li>{x}</li>" for x in out)


def evidence_table(p):
    loc = p.get("location")
    cols = []
    if loc:
        cols.append(("Website", {
            "Name": loc["name"], "Parent": "Bellhaven Senior Living", "Address": f'{loc["street"]}, {loc["city"]}, {loc["state"]} {loc["zip"]}',
            "Care": ", ".join(loc["care_offerings"]), "Status": "listed on website", "Phone": loc["phone"],
            "Revenue / AR": "", "Contacts": f'Administrator: {loc["administrator"]}',
        }, f'<a href="{escape(loc["url"])}" target="_blank">website page</a>'))
    for a in p["accounts"]:
        cols.append((a["role"], {
            "Name": a["name"], "Parent": a["parent_name"], "Address": a["address"], "Care": a["care_type"],
            "Status": a["status"], "Phone": a["phone"],
            "Revenue / AR": f'${a["lifetime_revenue"]:,} / ${a["outstanding_ar"]:,}',
            "Contacts": "; ".join(a["contacts"]) or "none",
        }, crm_link(a["account_id"])))
    rows = ["Name", "Parent", "Address", "Care", "Status", "Phone", "Revenue / AR", "Contacts"]
    head = "".join(f"<th>{escape(t)}<br><span class=muted>{link}</span></th>" for t, _, link in cols)
    body = ""
    for r in rows:
        first = cols[0][1].get(r, "") if cols else ""
        cells = ""
        for i, (_, vals, _) in enumerate(cols):
            v = str(vals.get(r, ""))
            diff = loc and i > 0 and r in ("Name", "Parent", "Status") and v and first and v != first and r != "Status"
            cells += f'<td class="{"diff" if diff else ""}">{escape(v)}</td>'
        body += f"<tr><th>{r}</th>{cells}</tr>"
    return f"<table><tr><th></th>{head}</tr>{body}</table>"


def card(row, decided=False):
    p = row["payload"]
    labels = "".join(f'<span class="pill">{escape(l)}</span>' for l in p["labels"])
    html = (f'<div class="card" id="p{row["id"]}"><h3>#{row["id"]} {escape(p["title"])}</h3>'
            f'<span class="pill k-{p["kind"]}">{KIND_LABEL[p["kind"]]}</span>{labels}'
            f'<span class="pill conf-{p["confidence"]}">confidence: {p["confidence"]}</span>'
            f'<h4>Why</h4><ul>{"".join(f"<li>{escape(r)}</li>" for r in p["reasons"])}</ul>'
            f'<h4>Evidence</h4>{evidence_table(p)}'
            f'<h4>{"What was done" if row["status"] == "applied" else "Approving will"}</h4>'
            f'<ul class="act">{describe_actions(p, row.get("result"))}</ul>')
    if decided:
        res = row.get("result")
        steps = res.get("steps") if isinstance(res, dict) else ([res] if res else [])
        html += (f'<p><b>{escape(row["status"].upper())}</b> by {escape(row["decided_by"] or "")} at {escape(row["decided_at"] or "")}'
                 f'{" · reason: " + escape(row["reason"]) if row.get("reason") else ""}</p>'
                 + (f'<ul class="muted">{"".join(f"<li>{escape(str(s))}</li>" for s in steps)}</ul>' if steps else ""))
    else:
        html += (f'<div class="btns"><form method="post" action="/p/{row["id"]}/approve">'
                 f'<button class="approve">Approve &amp; write to CRM</button></form>'
                 f'<form method="post" action="/p/{row["id"]}/reject"><input type="text" name="reason" placeholder="Reason for rejecting (optional)">'
                 f' <button class="reject">Reject</button></form></div>')
    return html + "</div>"


def page(title, body, active, flash=""):
    c = store.counts()
    last = store.last_run()
    nav = "".join(f'<a class="{"on" if active == k else ""}" href="{u}">{t}</a>'
                  for k, u, t in [("queue", "/", f'Review queue ({c.get("pending", 0)})'),
                                  ("decided", "/decided", "Decided"), ("matched", "/matched", "Confident matches")])
    stats = "".join(f'<div class="stat"><b>{c.get(k, 0)}</b>{k}</div>' for k in ["pending", "applied", "rejected", "superseded", "stale", "failed"])
    run_info = ""
    if last:
        s = last["summary"]
        run_info = (f'<p class="muted">Last run {escape(last["finished_at"])}: {s["website_locations"]} website locations, '
                    f'{s["crm_accounts"]} CRM accounts, {s["confident_matches_no_change"]} already correct, '
                    f'{s["proposals_this_run"]} proposals ({escape(json.dumps(s.get("queue", {})))}).<br>'
                    + "<br>".join(escape(n) for n in s["site_notes"] + s["warnings"]) + "</p>")
    f = ""
    if flash:
        f = f'<div class="flash {"err" if flash.startswith("!") else ""}">{escape(flash.lstrip("!"))}</div>'
    return (f"<!doctype html><html><head><meta charset=utf-8><title>{escape(title)}</title><style>{CSS}</style></head><body>"
            f'<header><h1>Bellhaven → CRM sync · review</h1>{nav}'
            f'<a href="{escape(config.crm_browser_url())}" target="_blank">CRM browser ↗</a>'
            f'<form method="post" action="/run" style="margin-left:auto"><button class="run">Run pipeline now</button></form></header>'
            f'<div class="wrap">{f}<div class="stats">{stats}</div>{run_info}{body}</div></body></html>')


class Handler(BaseHTTPRequestHandler):
    def _send(self, html, code=200):
        data = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, to):
        self.send_response(303)
        self.send_header("Location", to)
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        flash = parse_qs(u.query).get("msg", [""])[0]
        if u.path == "/":
            rows = store.proposals(["pending"])
            body = ""
            for k in KIND_ORDER:
                group = [r for r in rows if r["kind"] == k]
                if group:
                    body += f"<h2>{KIND_LABEL[k]} ({len(group)})</h2>" + "".join(card(r) for r in group)
            body = body or "<p>Nothing waiting for review. Run the pipeline to check for changes.</p>"
            self._send(page("Review queue", body, "queue", flash))
        elif u.path == "/decided":
            rows = store.proposals(["applied", "rejected", "superseded", "stale", "failed"])
            rows.sort(key=lambda r: r.get("decided_at") or "", reverse=True)
            self._send(page("Decided", "".join(card(r, decided=True) for r in rows) or "<p>No decisions yet.</p>", "decided", flash))
        elif u.path == "/matched":
            path = config.REPORT_DIR / "latest.json"
            data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"confident": []}
            trs = "".join(
                f'<tr><td>{escape(m["location"]["name"])}</td><td>{escape(m["location"]["street"])}, {escape(m["location"]["city"])}, {escape(m["location"]["state"])}</td>'
                f'<td>{crm_link(m["account"]["account_id"], m["account"]["name"])}</td><td>{escape(m["account"]["parent_name"])}</td>'
                f'<td>{escape("; ".join(m["reasons"]))}</td></tr>' for m in data["confident"])
            body = (f'<h2>Confident matches, nothing to change ({len(data["confident"])})</h2>'
                    f'<table><tr><th>Website</th><th>Address</th><th>CRM account</th><th>Parent</th><th>Matched on</th></tr>{trs}</table>')
            self._send(page("Confident matches", body, "matched", flash))
        else:
            self._send("Not found", 404)

    def do_POST(self):
        u = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode()) if length else {}
        parts = u.path.strip("/").split("/")
        try:
            if u.path == "/run":
                summary, _, _ = run_pipeline.run(store=store, quiet=True)
                q = summary["queue"]
                msg = (f'Pipeline ran: {q["new"]} new, {q["refreshed"]} still pending, '
                       f'{q["skipped_already_decided"]} skipped (already decided), {q["superseded"]} superseded.')
                return self._redirect("/?msg=" + quote(msg))
            if len(parts) == 3 and parts[0] == "p" and parts[2] in ("approve", "reject"):
                pid = int(parts[1])
                row = store.get(pid)
                if not row or row["status"] != "pending":
                    return self._redirect("/?msg=" + quote(f"!Proposal #{pid} is not pending."))
                if parts[2] == "reject":
                    store.decide(pid, "rejected", REVIEWER, form.get("reason", [""])[0])
                    return self._redirect("/?msg=" + quote(f"Rejected #{pid}. It won't be proposed again."))
                client = CRMClient(read_only=False)
                try:
                    result = apply(client, row["payload"], log=lambda op, t, d: store.log(pid, op, t, d))
                    store.decide(pid, "applied", REVIEWER, "", result)
                    return self._redirect("/decided?msg=" + quote(f"Approved #{pid}: " + "; ".join(result["steps"])) + f"#p{pid}")
                except StaleProposal as e:
                    store.decide(pid, "stale", REVIEWER, "", str(e))
                    return self._redirect("/?msg=" + quote(f"!#{pid} not applied, the CRM changed since it was proposed: {e}. Re-run the pipeline."))
        except Exception as e:
            traceback.print_exc()
            if len(parts) == 3 and parts[0] == "p":
                store.decide(int(parts[1]), "failed", REVIEWER, "", f"{type(e).__name__}: {e}")
            return self._redirect("/?msg=" + quote(f"!Error: {e}"))
        self._send("Not found", 404)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s\n" % (fmt % args))


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else int(os.environ.get("PORT", 8000))
    print(f"Review app on http://127.0.0.1:{port}  (reviewer: {REVIEWER})")
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
