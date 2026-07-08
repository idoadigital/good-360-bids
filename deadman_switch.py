"""Dead-man's switch.

Run this as a *separate* cron (every 5 min) on an independent host or
in an independent container. If the main monitor's heartbeat stops updating,
this process sends a Telegram alert — even when the whole main box is down.

This is the postmortem lesson: the old watchdog ran on the same host as the
thing it was watching, so when the host went dark, nobody got paged. The fix
is a probe that lives somewhere else and fails loud by phoning home.

Usage:
    # On independent host/container, every 5 min
    TELEGRAM_BOT_TOKEN=... HEARTBEAT_URL=... python deadman_switch.py

HEARTBEAT_URL should be a read endpoint on the main system that returns the
heartbeat JSON. If you prefer filesystem sharing, set HEARTBEAT_FILE instead.
Exactly one of the two must be set.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from pathlib import Path

import requests

MAX_STALE_MINUTES = int(os.environ.get("DEADMAN_MAX_STALE_MINUTES", "10"))
STATE_FILE = Path(os.environ.get("DEADMAN_STATE", "/tmp/deadman_state.json"))
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_OPERATOR_CHAT_ID", "")


def fetch_heartbeat() -> dict | None:
    """Return parsed heartbeat dict, or None if unreachable."""
    url = os.environ.get("HEARTBEAT_URL")
    path = os.environ.get("HEARTBEAT_FILE")
    if url:
        try:
            r = requests.get(url, timeout=5)
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            return None
    if path:
        p = Path(path)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return None
    raise RuntimeError("Set HEARTBEAT_URL or HEARTBEAT_FILE")


def heartbeat_age(hb: dict | None) -> timedelta | None:
    if not hb:
        return None
    raw = hb.get("last_scan") or hb.get("last_success")
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return datetime.now(UTC) - ts


def load_state() -> dict:
    if not STATE_FILE.exists():
        return {"alerted": False}
    try:
        return json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {"alerted": False}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state))


def send_alert(message: str) -> None:
    """Admin alert via the channel router. On the independent probe host
    dashboard.db is unreachable, so the router degrades to the legacy
    TELEGRAM_OPERATOR_CHAT_ID env destination by itself. If even the router
    module is missing (stripped-down probe deploy), fall back to a direct
    operator-chat send — the dead-man's switch must fail loud, not silent."""
    try:
        import telegram_router
    except ImportError:
        telegram_router = None
    if telegram_router is not None:
        if not telegram_router.send(telegram_router.ADMIN, message, source='deadman'):
            print(f"[DEADMAN] Telegram not delivered — message was: {message}", file=sys.stderr)
        return
    delivered = False
    err = None
    if not (BOT_TOKEN and CHAT_ID):
        err = "no telegram config"
        print(f"[DEADMAN] No telegram config — would have sent: {message}", file=sys.stderr)
    else:
        try:
            requests.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                json={"chat_id": CHAT_ID, "text": message, "parse_mode": "HTML"},
                timeout=10,
            )
            delivered = True
        except requests.RequestException as e:
            err = str(e)
            print(f"[DEADMAN] Telegram send failed: {e}", file=sys.stderr)
    try:
        from notifications_log import record_telegram
        record_telegram(source='deadman', message=message, delivered=delivered, error=err, channel='admin')
    except Exception:
        pass


def main() -> int:
    hb = fetch_heartbeat()
    age = heartbeat_age(hb)
    state = load_state()
    already_alerted = state.get("alerted", False)

    if age is None or age > timedelta(minutes=MAX_STALE_MINUTES):
        if already_alerted:
            print("[DEADMAN] still down — alert already sent")
            return 1
        reason = "heartbeat unreachable" if age is None else f"heartbeat stale by {age.total_seconds():.0f}s"
        send_alert(
            f"🚨 <b>DEAD-MAN'S SWITCH TRIPPED</b>\n\n"
            f"Main monitor {reason}.\n"
            f"This alert came from the independent probe.\n"
            f"<i>Investigate the main host immediately.</i>"
        )
        save_state({"alerted": True, "since": datetime.now(UTC).isoformat()})
        return 1

    if already_alerted:
        send_alert(
            f"✅ <b>DEAD-MAN'S SWITCH CLEARED</b>\n\n"
            f"Heartbeat fresh again (age {age.total_seconds():.0f}s)."
        )
        save_state({"alerted": False})
    print(f"[DEADMAN] heartbeat age {age.total_seconds():.0f}s — OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
