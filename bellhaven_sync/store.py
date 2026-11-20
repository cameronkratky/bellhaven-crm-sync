"""SQLite state: every proposal ever made, every decision, every field written.

Re-run safety lives here. A proposal's fingerprint is stable across runs, so:
  - already approved (applied) or rejected  -> never proposed again
  - still pending                           -> refreshed in place, not duplicated
  - no longer produced by the latest run    -> marked superseded (the data changed)
  - failed or stale                         -> re-opened for review
"""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
  id INTEGER PRIMARY KEY, started_at TEXT, finished_at TEXT, summary TEXT);
CREATE TABLE IF NOT EXISTS proposals (
  id INTEGER PRIMARY KEY, fingerprint TEXT UNIQUE, kind TEXT, title TEXT,
  payload TEXT, status TEXT DEFAULT 'pending', first_run INTEGER, last_run INTEGER,
  decided_at TEXT, decided_by TEXT, reason TEXT, result TEXT);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY, proposal_id INTEGER, at TEXT, op TEXT, target TEXT, detail TEXT);
"""
DECIDED = ("applied", "rejected")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def start_run(self):
        cur = self.db.execute("INSERT INTO runs (started_at) VALUES (?)", (now(),))
        self.db.commit()
        return cur.lastrowid

    def finish_run(self, run_id, summary):
        self.db.execute("UPDATE runs SET finished_at=?, summary=? WHERE id=?", (now(), json.dumps(summary), run_id))
        self.db.commit()

    def save_proposals(self, run_id, proposals):
        counts = {"new": 0, "refreshed": 0, "skipped_already_decided": 0, "reopened": 0, "superseded": 0}
        seen = set()
        for p in proposals:
            fp = p["fingerprint"]
            seen.add(fp)
            row = self.db.execute("SELECT id, status FROM proposals WHERE fingerprint=?", (fp,)).fetchone()
            if row is None:
                self.db.execute("INSERT INTO proposals (fingerprint, kind, title, payload, status, first_run, last_run) "
                                "VALUES (?,?,?,?, 'pending', ?, ?)", (fp, p["kind"], p["title"], json.dumps(p), run_id, run_id))
                counts["new"] += 1
            elif row["status"] in DECIDED:
                counts["skipped_already_decided"] += 1
            else:
                reopened = row["status"] != "pending"
                self.db.execute("UPDATE proposals SET payload=?, status='pending', last_run=?, "
                                "result=CASE WHEN ? THEN NULL ELSE result END WHERE id=?",
                                (json.dumps(p), run_id, reopened, row["id"]))
                counts["reopened" if reopened else "refreshed"] += 1
        for row in self.db.execute("SELECT id, fingerprint FROM proposals WHERE status='pending'").fetchall():
            if row["fingerprint"] not in seen:
                self.db.execute("UPDATE proposals SET status='superseded', result=? WHERE id=?",
                                ("No longer produced by the latest run (CRM or website changed).", row["id"]))
                counts["superseded"] += 1
        self.db.commit()
        return counts

    def proposals(self, statuses):
        q = f"SELECT * FROM proposals WHERE status IN ({','.join('?' * len(statuses))}) ORDER BY id"
        return [self._row(r) for r in self.db.execute(q, statuses).fetchall()]

    def get(self, pid):
        r = self.db.execute("SELECT * FROM proposals WHERE id=?", (pid,)).fetchone()
        return self._row(r) if r else None

    def decide(self, pid, status, by, reason="", result=None):
        self.db.execute("UPDATE proposals SET status=?, decided_at=?, decided_by=?, reason=?, result=? WHERE id=?",
                        (status, now(), by, reason, json.dumps(result) if result is not None else None, pid))
        self.db.commit()

    def log(self, pid, op, target, detail):
        self.db.execute("INSERT INTO audit (proposal_id, at, op, target, detail) VALUES (?,?,?,?,?)",
                        (pid, now(), op, target, json.dumps(detail)))
        self.db.commit()

    def counts(self):
        return {r["status"]: r["n"] for r in self.db.execute("SELECT status, COUNT(*) n FROM proposals GROUP BY status")}

    def last_run(self):
        r = self.db.execute("SELECT * FROM runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return dict(r) | {"summary": json.loads(r["summary"])} if r else None

    @staticmethod
    def _row(r):
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        d["result"] = json.loads(d["result"]) if d.get("result") and d["result"].startswith(("[", "{")) else d.get("result")
        return d
