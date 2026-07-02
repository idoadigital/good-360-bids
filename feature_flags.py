"""Environment-level feature flags for the multi-environment workflow.

Staging / feature environments set ENABLE_AUTO_BUY=false and
ENABLE_URL_SCANNING=false in their .env so the sensitive machinery can
never run outside production. Flags default to ENABLED when unset so
production behaves identically without any .env change.

These flags are read from os.environ ONLY. They are deliberately NOT in
settings_bootstrap._KEYS_TO_LOAD, so a value in the dashboard settings DB
can never override what the environment's .env says.
"""
from __future__ import annotations

import os

_FALSY = {"false", "0", "no", "off"}
_TRUTHY = {"true", "1", "yes", "on"}


def flag_enabled(name: str, default: bool = True) -> bool:
    """Return the boolean value of an env feature flag.

    Unset or unrecognizable values fall back to `default` (enabled for the
    production flags — prod runs without these vars in its .env).
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    value = raw.strip().lower()
    if value in _FALSY:
        return False
    if value in _TRUTHY:
        return True
    return default


def auto_buy_enabled() -> bool:
    """Master switch for every purchase-capable code path."""
    return flag_enabled("ENABLE_AUTO_BUY", default=True)


def url_scanning_enabled() -> bool:
    """Master switch for the Good360 scan loop."""
    return flag_enabled("ENABLE_URL_SCANNING", default=True)


def notifications_enabled() -> bool:
    """Master switch for every outbound notification transport (Telegram,
    email/SMTP, SMS). Only production may send — staging/feature set
    ENABLE_NOTIFICATIONS=false so operators and customers never get
    double/phantom messages from non-prod stacks."""
    return flag_enabled("ENABLE_NOTIFICATIONS", default=True)


def notifications_blocked_msg(channel: str) -> str:
    """Uniform log line for skipped sends, greppable across services."""
    return (f"[NOTIFICATIONS DISABLED] {channel} send skipped "
            "(ENABLE_NOTIFICATIONS=false in this environment)")
