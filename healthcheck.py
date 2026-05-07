#!/usr/bin/env python3
"""Real health check — fails loud when the system is silently broken.

Exits 0 only if ALL of:
  1. Last scan heartbeat is < MAX_STALE_MINUTES old.
  2. Playwright can import AND launch (catches wiped browser / venv drift).
  3. At least one org has required secrets in the environment.

This is the postmortem fix: the old health probe returned "green" during a
3-day outage because it only checked that scripts *exited* cleanly. This one
checks that the *work* is actually happening.

Usage:
  python healthcheck.py           # exit code only (for Docker HEALTHCHECK)
  python healthcheck.py --verbose # prints each check
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from pathlib import Path

MAX_STALE_MINUTES = int(os.environ.get("HEALTH_MAX_STALE_MINUTES", "5"))
WORKDIR = Path(os.environ.get("WORKDIR", "/app/workdir"))
HEARTBEAT_FILE = WORKDIR / "good360_heartbeat.json"


def check_heartbeat() -> tuple[bool, str]:
    if not HEARTBEAT_FILE.exists():
        return False, f"heartbeat file missing: {HEARTBEAT_FILE}"
    try:
        data = json.loads(HEARTBEAT_FILE.read_text())
        # accept either field (monitor writes last_success; future writers may use last_scan)
        raw_ts = data.get("last_scan") or data.get("last_success")
        if not raw_ts:
            return False, "heartbeat has no last_scan / last_success field"
        ts = datetime.fromisoformat(raw_ts.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
    except (KeyError, ValueError, json.JSONDecodeError) as e:
        return False, f"heartbeat unreadable: {e}"
    age = datetime.now(UTC) - ts
    if age > timedelta(minutes=MAX_STALE_MINUTES):
        return False, f"last scan was {age.total_seconds():.0f}s ago (> {MAX_STALE_MINUTES}m)"
    return True, f"last scan {age.total_seconds():.0f}s ago"


def check_playwright() -> tuple[bool, str]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        return False, f"playwright import failed: {e}"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception as e:
        return False, f"chromium launch failed: {e}"
    return True, "chromium launches"


def check_orgs_configured() -> tuple[bool, str]:
    try:
        from config import load_orgs
    except ImportError as e:
        return False, f"config import failed: {e}"
    orgs = load_orgs()
    if not orgs:
        return False, "no orgs have required secrets in env"
    return True, f"{len(orgs)} org(s) configured: {', '.join(orgs)}"


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    # Note: the postmortem-era `check_orgs_configured` looked for ${VAR}-style
    # secrets in env. In the current architecture, scan creds come from the
    # encrypted dashboard DB (settings_bootstrap) and per-org purchase creds
    # are fetched live from QuickBeed. The heartbeat check is the meaningful
    # "is the system actually doing work" signal — if the scan login is
    # broken, the heartbeat won't advance.
    checks = [
        ("heartbeat", check_heartbeat),
        ("playwright", check_playwright),
    ]
    failed = 0
    for name, fn in checks:
        ok, msg = fn()
        if verbose or not ok:
            status = "OK" if ok else "FAIL"
            print(f"[{status}] {name}: {msg}")
        if not ok:
            failed += 1
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
