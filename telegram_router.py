"""Telegram multi-channel router — the single outbound Telegram sender.

Replaces the ad-hoc requests.post(api.telegram.org/...) blocks that used to
live in monitor / autobuy / watchdog / report / readiness / intake. Routing
categories:

  ADMIN   — errors, incidents, payment failures, self-heal notices.
            Operator-only channels.
  GENERAL — truck availability alerts (no customer data). Falls back to
            ADMIN channels when no general channel is configured.
  NGO     — per-customer messages; pass org_id and/or org_key. Routes to
            the enabled ngo channel(s) matching that org. If none match,
            the message goes to the ADMIN channels instead. A customer-
            attributable message is NEVER broadcast to other orgs'
            channels — the legacy all-orgs fan-out is gone.
  GROUP   — opt-in broadcast channels. Nothing routes here automatically;
            callers must pass group_channel_ids explicitly.

The channel registry is the telegram_channels table in dashboard.db
(managed from the admin UI: Settings → Telegram Channels). Where the
registry can't be read (deadman probe host, containers without the
dashboard modules) the router degrades to the legacy
TELEGRAM_OPERATOR_CHAT_ID env destination — admin-only, never an org group.

Never raises into callers; one channel's HTTP failure doesn't block the
others. Every send (or gated skip) is recorded via
notifications_log.record_telegram with a truthful channel label:
admin | general | ngo:<org_key-or-id> | group:<channel-id>.
"""
from __future__ import annotations

import os
import sys

import requests

ADMIN = "admin"
GENERAL = "general"
NGO = "ngo"
GROUP = "group"

_CATEGORIES = (ADMIN, GENERAL, NGO, GROUP)


def _notifications_enabled() -> bool:
    """ENABLE_NOTIFICATIONS env master switch (false in staging/feature).
    Same fallback shim pattern as order_verifier.py — env-only when the
    feature_flags module isn't importable."""
    try:
        _root = os.path.dirname(os.path.abspath(__file__))
        if _root not in sys.path:
            sys.path.insert(0, _root)
        import feature_flags
        return feature_flags.notifications_enabled()
    except Exception:
        return os.environ.get("ENABLE_NOTIFICATIONS", "true").strip().lower() not in (
            "false", "0", "no", "off")


def _bot_token() -> str:
    """TELEGRAM_BOT_TOKEN from env — hydrated by settings_bootstrap in the
    long-running scripts, exactly like the legacy senders. Best-effort
    bootstrap import for processes that haven't loaded it yet."""
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    if token:
        return token
    try:
        import settings_bootstrap  # noqa: F401  (auto-loads on import)
    except Exception:
        return ""
    return (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()


def _get_conn():
    """Locate missioncontrol/db.py's get_conn the same way
    notifications_log does. Returns None when unavailable."""
    candidates = [
        "/app/missioncontrol",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "missioncontrol"),
    ]
    for path in candidates:
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    try:
        from db import get_conn  # type: ignore
    except ImportError:
        return None
    return get_conn


def _load_channels():
    """All enabled registry rows as dicts, or None when the registry is
    unreachable (missing DB / module / table)."""
    try:
        get_conn = _get_conn()
        if get_conn is None:
            return None
        with get_conn() as c:
            rows = c.execute(
                """SELECT id, chat_id, title, category, org_id, org_key
                   FROM telegram_channels WHERE enabled = 1"""
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception:
        return None


def _env_fallback_channels():
    """Registry unreachable → degrade to the legacy operator chat only."""
    op = (os.environ.get("TELEGRAM_OPERATOR_CHAT_ID") or "").strip()
    if not op:
        return []
    return [{"id": None, "chat_id": op, "title": "Operator (env fallback)",
             "category": ADMIN, "org_id": None, "org_key": None}]


def resolve_channels(category, *, org_id=None, org_key=None, fallback=True):
    """Read-only: the enabled channel rows a send() with these args would
    target. With fallback=False, an NGO lookup returns [] instead of the
    admin channels when the org has no channel — callers use this to decide
    whether an extra ADMIN mirror send would duplicate the fallback."""
    category = str(category or "").strip().lower()
    channels = _load_channels()
    if channels is None:
        channels = _env_fallback_channels()
    if category == NGO:
        matched = [c for c in channels
                   if c["category"] == NGO and _match_org(c, org_id, org_key)]
        if matched or not fallback:
            return matched
        return [c for c in channels if c["category"] == ADMIN]
    return [c for c in channels if c["category"] == category]


def _match_org(ch, org_id, org_key):
    if org_key and ch.get("org_key") and str(ch["org_key"]) == str(org_key):
        return True
    if org_id is not None and ch.get("org_id") is not None \
            and str(ch["org_id"]) == str(org_id):
        return True
    return False


def _post(token, chat_id, text, parse_mode):
    """One HTTP send. Returns (delivered, error). Never raises."""
    try:
        payload = {"chat_id": chat_id, "text": text,
                   "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload, timeout=10)
        try:
            result = resp.json()
        except ValueError:
            return False, f"HTTP {resp.status_code}: {str(resp.text)[:200]}"
        if result.get("ok"):
            return True, None
        return False, str(result)[:500]
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _record(source, message, delivered, error, channel, level, title):
    try:
        import notifications_log
        notifications_log.record_telegram(
            source=source, message=message, delivered=delivered, error=error,
            channel=channel, level=level, title=title)
    except Exception:
        pass


def _update_channel(channel_id, delivered, error):
    if channel_id is None:
        return
    try:
        get_conn = _get_conn()
        if get_conn is None:
            return
        with get_conn() as c:
            c.execute(
                """UPDATE telegram_channels
                   SET last_sent_at = datetime('now'), last_error = ?
                   WHERE id = ?""",
                (None if delivered else str(error or "send failed")[:500],
                 channel_id))
    except Exception:
        pass


def send(category, message, *, org_id=None, org_key=None, title=None,
         source="system", level="info", parse_mode="HTML",
         group_channel_ids=None):
    """Route one message to its category's channels.
    Returns True if at least one channel accepted it. Never raises."""
    try:
        return _send(category, message, org_id, org_key, title, source,
                     level, parse_mode, group_channel_ids)
    except Exception as e:
        print(f"[TELEGRAM ROUTER] send failed: {type(e).__name__}: {e}")
        return False


def _send(category, message, org_id, org_key, title, source, level,
          parse_mode, group_channel_ids):
    category = str(category or "").strip().lower()
    if category not in _CATEGORIES:
        print(f"[TELEGRAM ROUTER] unknown category {category!r} — dropped")
        return False
    if not _notifications_enabled():
        print(f"[NOTIFICATIONS DISABLED] telegram/{category} send skipped "
              "(ENABLE_NOTIFICATIONS=false in this environment)")
        _record(source, message, False, "skipped: ENABLE_NOTIFICATIONS=false",
                category, level, title)
        return False

    channels = _load_channels()
    if channels is None:
        channels = _env_fallback_channels()
    admin_targets = [c for c in channels if c["category"] == ADMIN]

    # (channel, truthful label) pairs.
    if category == ADMIN:
        targets = [(c, "admin") for c in admin_targets]
    elif category == GENERAL:
        gen = [c for c in channels if c["category"] == GENERAL]
        targets = ([(c, "general") for c in gen]
                   or [(c, "admin") for c in admin_targets])
    elif category == NGO:
        matched = [c for c in channels
                   if c["category"] == NGO and _match_org(c, org_id, org_key)]
        if matched:
            targets = [(c, f"ngo:{c.get('org_key') or c.get('org_id')}")
                       for c in matched]
        else:
            # No channel for this org (or no org given): deliver to the
            # operator's admin channels. NEVER to other orgs' channels.
            targets = [(c, "admin") for c in admin_targets]
    else:  # GROUP — explicit opt-in only, no automatic routing
        ids = {str(i) for i in (group_channel_ids or [])}
        targets = [(c, f"group:{c['id']}") for c in channels
                   if c["category"] == GROUP and str(c["id"]) in ids]

    if not targets:
        err = f"no enabled telegram channels for category '{category}'"
        print(f"[TELEGRAM ROUTER] {err}")
        _record(source, message, False, err, category, level, title)
        return False

    token = _bot_token()
    if not token:
        print("[TELEGRAM ROUTER] TELEGRAM_BOT_TOKEN not set — cannot send")
        _record(source, message, False, "TELEGRAM_BOT_TOKEN not set",
                category, level, title)
        return False

    any_delivered = False
    for ch, label in targets:
        delivered, err = _post(token, ch["chat_id"], message, parse_mode)
        any_delivered = any_delivered or delivered
        _record(source, message, delivered, err, label, level, title)
        _update_channel(ch.get("id"), delivered, err)
    return any_delivered


def send_to_channel(channel_id, message, *, source="admin-test", title=None,
                    level="info", parse_mode="HTML"):
    """Send to ONE specific registry channel (the admin UI's test ping).
    Returns (delivered, error). Never raises. The environment notifications
    gate still applies — non-prod stacks can't ping real chats."""
    try:
        if not _notifications_enabled():
            return False, ("notifications disabled in this environment "
                           "(ENABLE_NOTIFICATIONS=false)")
        get_conn = _get_conn()
        if get_conn is None:
            return False, "dashboard db unavailable"
        with get_conn() as c:
            row = c.execute(
                """SELECT id, chat_id, title, category, org_id, org_key
                   FROM telegram_channels WHERE id = ?""",
                (channel_id,)).fetchone()
        if not row:
            return False, "channel not found"
        ch = dict(row)
        token = _bot_token()
        if not token:
            return False, "TELEGRAM_BOT_TOKEN not set"
        delivered, err = _post(token, ch["chat_id"], message, parse_mode)
        if ch["category"] == NGO:
            label = f"ngo:{ch.get('org_key') or ch.get('org_id')}"
        elif ch["category"] == GROUP:
            label = f"group:{ch['id']}"
        else:
            label = ch["category"]
        _record(source, message, delivered, err, label, level, title)
        _update_channel(ch["id"], delivered, err)
        return delivered, err
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
