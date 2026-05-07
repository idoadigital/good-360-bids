"""Lightweight notifications recorder.

Each `send_telegram*` function in the codebase calls `record_telegram(...)`
from here, so the admin dashboard can surface every alert that was pushed to
Telegram on a "Notifications" page.

Design:
- This module is import-safe: if the dashboard DB isn't reachable (no
  DASHBOARD_MASTER_KEY, missing missioncontrol package, sqlite path missing,
  etc.) the call returns silently. A logger should never break the sender.
- We only require sqlite + the `notifications` table from missioncontrol/db.py.
  No encryption: notification bodies are not secrets — they're the same text
  that already flows to Telegram chats.
- Level / title are inferred from the message body when not supplied so the
  call sites stay short.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from typing import Optional


def _infer_level(message: str) -> str:
    msg = message or ""
    if "❌" in msg or "ERROR" in msg.upper() or "FAILED" in msg.upper() or "TIMEOUT" in msg.upper():
        return "error"
    if "⚠" in msg or "WARN" in msg.upper() or "MISSED" in msg.upper():
        return "warn"
    if "✅" in msg or "SUCCESS" in msg.upper() or "PURCHASED" in msg.upper():
        return "success"
    return "info"


def _infer_title(message: str) -> str:
    if not message:
        return ""
    first = message.strip().split("\n", 1)[0].strip()
    return first[:200]


def _get_conn():
    # Try to reuse missioncontrol/db.py's connection helper. Add the
    # /app/missioncontrol bind-mount path the same way settings_bootstrap
    # does, since legacy scripts may not have it on sys.path.
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


def record_telegram(
    *,
    source: str,
    message: str,
    delivered: bool = True,
    error: Optional[str] = None,
    channel: Optional[str] = None,
    level: Optional[str] = None,
    title: Optional[str] = None,
) -> None:
    """Persist one Telegram outbound message. Never raises."""
    try:
        get_conn = _get_conn()
        if get_conn is None:
            return
        ts = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with get_conn() as c:
            c.execute(
                """INSERT INTO notifications
                   (ts, source, level, channel, title, message, delivered, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ts,
                    source,
                    level or _infer_level(message),
                    channel,
                    title or _infer_title(message),
                    message or "",
                    1 if delivered else 0,
                    error,
                ),
            )
    except Exception:
        # Best-effort logging; never break the caller.
        pass
