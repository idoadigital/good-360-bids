"""Record login attempts to the dashboard DB so the admin UI can show
a live "logged in successfully / failed" panel.

Designed to be safe to import from any container: silently no-ops if the
dashboard DB isn't reachable. A logger must never break the caller.

Usage:
    from login_telemetry import record_login_attempt
    record_login_attempt(source="monitor", email=GOOD360_EMAIL,
                         success=True, duration_ms=4321)
"""
from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from typing import Optional


def _get_conn():
    candidates = [
        "/app/missioncontrol",
        os.path.join(os.path.dirname(os.path.abspath(__file__))),
    ]
    for p in candidates:
        if os.path.isdir(p) and p not in sys.path:
            sys.path.insert(0, p)
    try:
        from db import get_conn  # type: ignore
        return get_conn
    except ImportError:
        return None


def record_login_attempt(
    *,
    source: str,
    email: Optional[str],
    success: bool,
    duration_ms: Optional[int] = None,
    error: Optional[str] = None,
) -> None:
    """Append one login-attempt row. Never raises."""
    try:
        get_conn = _get_conn()
        if get_conn is None:
            return
        # Trim error blobs so we don't store the whole HTML page on a failure.
        err = (error or "")
        if len(err) > 4000:
            err = err[:2000] + " …[truncated]… " + err[-1000:]
        with get_conn() as c:
            c.execute(
                """INSERT INTO good360_login_attempts (source, email, success, duration_ms, error)
                   VALUES (?, ?, ?, ?, ?)""",
                (source, email or "", 1 if success else 0, duration_ms, err or None),
            )
    except Exception:
        pass


@contextmanager
def measured_login(*, source: str, email: Optional[str]):
    """Context manager that records the outcome automatically.

    Usage:
        with measured_login(source="monitor", email=EMAIL) as lo:
            # do the playwright login here
            lo.set_success()       # call on success
            # raising on failure also records (error captured from exception)
    """
    started = time.monotonic()
    state = {"success": False, "error": None}

    class _Recorder:
        def set_success(self): state["success"] = True
        def set_error(self, msg: str): state["error"] = msg

    try:
        yield _Recorder()
    except Exception as exc:
        state["error"] = f"{type(exc).__name__}: {exc}"
        record_login_attempt(
            source=source, email=email, success=False,
            duration_ms=int((time.monotonic() - started) * 1000),
            error=state["error"],
        )
        raise
    record_login_attempt(
        source=source, email=email, success=state["success"],
        duration_ms=int((time.monotonic() - started) * 1000),
        error=state["error"],
    )
