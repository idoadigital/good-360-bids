"""Pull settings from the dashboard's encrypted SQLite store into process env.

Imported at the top of long-running scripts (good360_monitor.py, daemon, etc.)
so the operator can manage credentials via the dashboard UI without editing
.env files. Falls back silently to whatever's already in env if the dashboard
DB or its secrets module isn't available — the script's existing env-var
contract is preserved.

Keys loaded (each key falls back to whatever's in env if not in the DB):
  SCAN_GOOD360_EMAIL, SCAN_GOOD360_PASSWORD  — master scan login (READ-ONLY
                                                browsing — never used to
                                                place an order)
  OPENAI_API_KEY                              — devtools agent

Requires DASHBOARD_MASTER_KEY in env to decrypt. Without it, this is a no-op.

NOTE: We deliberately do NOT alias master scan credentials onto per-org env
variables. The master account is for browsing only; purchases must use the
selected customer's own credentials, fetched live from QuickBeed at the
moment of purchase.
"""
from __future__ import annotations

import os
import sys


_KEYS_TO_LOAD = (
    "SCAN_GOOD360_EMAIL",
    "SCAN_GOOD360_PASSWORD",
    "OPENAI_API_KEY",
    # Telegram alert credentials live in the encrypted dashboard DB so the
    # operator can rotate them through the admin UI. Hydrate into env so the
    # send_telegram* functions in good360_monitor / autobuy / watchdog /
    # report / deadman_switch can reach the API.
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_GROUP_HOPE4HUMANITY",
    "TELEGRAM_GROUP_REVIVING_HOMES",
    "TELEGRAM_OPERATOR_CHAT_ID",
)


def load() -> dict:
    """Populate `os.environ` from the dashboard settings table.
    Returns a dict describing which keys were loaded (for logging)."""
    if not os.environ.get("DASHBOARD_MASTER_KEY"):
        return {"loaded_from_db": [], "skipped": "DASHBOARD_MASTER_KEY not set"}

    # The dashboard modules live in /app/missioncontrol when running in any of
    # the docker containers, but adapt for local dev too.
    candidates = ["/app/missioncontrol", os.path.join(os.path.dirname(__file__), "missioncontrol")]
    for path in candidates:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)

    try:
        from db import get_conn
        import secrets_store
    except ImportError as exc:
        return {"loaded_from_db": [], "skipped": f"dashboard modules unavailable: {exc}"}

    loaded: list[str] = []
    try:
        with get_conn() as c:
            for key in _KEYS_TO_LOAD:
                row = c.execute(
                    "SELECT value_enc FROM settings WHERE key = ?", (key,)
                ).fetchone()
                if not row:
                    continue
                try:
                    value = secrets_store.decrypt(row["value_enc"])
                except Exception:  # noqa: BLE001
                    continue
                if value:
                    os.environ[key] = value
                    loaded.append(key)
    except Exception as exc:  # noqa: BLE001
        return {"loaded_from_db": loaded, "error": f"{type(exc).__name__}: {exc}"}

    return {"loaded_from_db": loaded}


# Auto-load on import so callers just `import settings_bootstrap` at the top.
_RESULT = load()
