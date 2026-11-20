"""Settings. The API token is read from the BELLHAVEN_TOKEN environment variable
(or a local .env file) and is never written to the repo."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv():
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip())


_load_dotenv()

BASE_URL = os.environ.get("BELLHAVEN_BASE_URL", "https://analyst-assessment-production.up.railway.app")
API_BASE = BASE_URL + "/api/v1"
TOKEN = os.environ.get("BELLHAVEN_TOKEN", "")

# The parent company whose locations we keep in sync, found in the CRM by this exact name.
PARENT_NAME = "Bellhaven Senior Living (Parent Account)"

DB_PATH = Path(os.environ.get("BELLHAVEN_DB", ROOT / "data" / "state.db"))
REPORT_DIR = ROOT / "data" / "reports"

# Marker we put at the start of every note we write, so re-runs can tell our notes apart.
NOTE_TAG = "[bellhaven-sync]"


def crm_browser_url(account_id: str = "") -> str:
    """Link to the read-only CRM browser for an account (the URL embeds the token)."""
    url = f"{BASE_URL}/crm/{TOKEN}/accounts"
    return f"{url}/{account_id}" if account_id else url
