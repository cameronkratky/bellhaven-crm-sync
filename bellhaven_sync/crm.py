"""Small client for the CRM sandbox API.

Guardrail: a client is read-only unless it is created with read_only=False.
The daily pipeline only ever builds a read-only client, so the only code path
that can write to the CRM is the review app's Approve button (applier.py).
"""
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import config


class ReadOnlyError(RuntimeError):
    pass


class CRMClient:
    def __init__(self, token: str = None, base: str = None, read_only: bool = True):
        self.base = base or config.API_BASE
        self.read_only = read_only
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token or config.TOKEN}"
        retry = Retry(total=4, backoff_factor=0.5, status_forcelist=[429, 500, 502, 503, 504],
                      allowed_methods=["GET"])  # never auto-retry writes
        self.session.mount("https://", HTTPAdapter(max_retries=retry))

    # ---------- reads ----------
    def _get(self, path, params=None):
        r = self.session.get(self.base + path, params=params, timeout=30)
        r.raise_for_status()
        return r.json()

    def _list_all(self, path, **params):
        rows, page = [], 1
        while True:
            body = self._get(path, {**params, "page": page, "page_size": 100})
            rows.extend(body["data"])
            if not body["data"] or len(rows) >= body["total"]:
                return rows
            page += 1

    def list_accounts(self, **filters):
        return self._list_all("/accounts", **filters)

    def get_account(self, account_id):
        return self._get(f"/accounts/{account_id}")

    def list_contacts(self, **filters):
        return self._list_all("/contacts", **filters)

    # ---------- writes (blocked on read-only clients) ----------
    def _write(self, method, path, payload):
        if self.read_only:
            raise ReadOnlyError(f"Refusing {method} {path}: this client is read-only. "
                                "Writes only happen from an approved proposal.")
        r = self.session.request(method, self.base + path, json=payload, timeout=30)
        if r.status_code >= 400:
            raise RuntimeError(f"{method} {path} failed {r.status_code}: {r.text[:300]}")
        return r.json()

    def create_account(self, fields):
        return self._write("POST", "/accounts", fields)["account_id"]

    def update_account(self, account_id, changes):
        return self._write("PATCH", f"/accounts/{account_id}", changes)

    def update_contact(self, contact_id, changes):
        return self._write("PATCH", f"/contacts/{contact_id}", changes)
