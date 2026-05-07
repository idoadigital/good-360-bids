"""Append-only audit log for money-moving events.

Every purchase attempt, card-data access, and admin action is appended to a
JSON-lines log. Never edit or delete entries — the log is the system-of-record
for "did we actually buy that truck and when."

Usage:
    from audit_log import audit
    audit("purchase_attempt", org_id="hope4humanity", truck="X", total=6399.00, status="SUCCESS")

The log rotates daily. If integrity matters (it does), also ship each day's
file to an immutable store (S3 Object Lock, Cloud Storage retention policy)
once rotated. That's out of scope here — wire it into your deploy.
"""
from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

_AUDIT_DIR = Path(os.environ.get("AUDIT_LOG_DIR", os.environ.get("WORKDIR", "/app/workdir") + "/audit"))
_HOST = socket.gethostname()


def _today_path() -> Path:
    _AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    return _AUDIT_DIR / f"audit-{datetime.now(UTC).strftime('%Y-%m-%d')}.jsonl"


def audit(event: str, **fields) -> None:
    """Append one structured event. Never raises — audit must not break the caller."""
    entry = {
        "ts": datetime.now(UTC).isoformat(),
        "host": _HOST,
        "pid": os.getpid(),
        "event": event,
        **fields,
    }
    try:
        with _today_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    except OSError:
        # Last-resort: print to stderr so at least container logs capture it.
        import sys
        print(f"[AUDIT-FAIL] {json.dumps(entry, default=str)}", file=sys.stderr)
