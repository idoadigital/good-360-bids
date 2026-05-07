"""
good360_bridge.py — E-Comsetter Good360 Roster System
Integration bridge between existing good360_monitor.py and roster_orchestrator.
Runs as a separate cron (every 2 min) — does NOT modify existing files.
Built: 2026-03-20
"""

import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# ─── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
ORCHESTRATOR = BASE_DIR / "roster_orchestrator.py"
STATE_FILE = BASE_DIR / "bridge_state.json"

# Path to the existing monitor DB (shared state)
# The monitor writes detected trucks somewhere — we'll check its DB / log
import os as _os

MONITOR_DB = Path(f"{_os.environ.get('WORKDIR', '/a0/usr/workdir')}/good360_roster/db/monitor_state.db")


def get_bridge_state():
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"last_processed_event_id": 0, "last_run": None}


def save_bridge_state(state: dict):
    state["last_run"] = datetime.utcnow().isoformat()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def get_latest_roster_event_id() -> int:
    """Get the latest truck_event id from the roster DB."""
    roster_db = BASE_DIR / "db" / "roster.db"
    if not roster_db.exists():
        return 0
    conn = sqlite3.connect(str(roster_db))
    try:
        row = conn.execute(
            "SELECT MAX(id) as max_id FROM truck_events"
        ).fetchone()
        return row[0] if row and row[0] else 0
    finally:
        conn.close()


def poll_for_new_events():
    """
    Poll for new truck events in the roster DB and process them via orchestrator.
    This is the main bridge loop — called every 2 minutes by cron.
    """
    state = get_bridge_state()
    last_id = state["last_processed_event_id"]

    roster_db = BASE_DIR / "db" / "roster.db"
    if not roster_db.exists():
        print("Roster DB not found — run schema.py first")
        return

    conn = sqlite3.connect(str(roster_db), timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """SELECT id, truck_title, truck_url, truck_price,
                     truck_location, truck_category, status
               FROM truck_events
               WHERE id > ? AND status = 'detected'
               ORDER BY id ASC LIMIT 10""",
            (last_id,)
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print(f"[{datetime.now()}] Bridge: No new events (last_id={last_id})")
        return

    print(f"Bridge: Found {len(rows)} new event(s) starting from id={last_id}")
    for row in rows:
        event_id = row["id"]
        print(f"  → Processing event #{event_id}: {row['truck_title']} [{row['truck_category']}] @ ${row['truck_price']}")

        # Call orchestrator
        result = subprocess.run(
            [sys.executable, str(ORCHESTRATOR), "process", str(event_id)],
            capture_output=True, text=True, timeout=300
        )
        if result.returncode == 0:
            print(f"  ✓ Event #{event_id} processed")
        else:
            print(f"  ✗ Event #{event_id} failed: {result.stderr[:200]}")

        state["last_processed_event_id"] = event_id
        save_bridge_state(state)


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="good360_bridge.py")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("poll", help="Poll and process new events once")
    sub.add_parser("install-cron", help="Print cron entry for this bridge")
    sub.add_parser("status", help="Show bridge status")

    args = parser.parse_args()

    if args.cmd == "poll" or not args.cmd:
        poll_for_new_events()
    elif args.cmd == "install-cron":
        print("# Add this line to crontab (crontab -e) to run the bridge:")
        print(f"*/2 * * * * cd {BASE_DIR} && {sys.executable} {BASE_DIR}/good360_bridge.py poll >> {BASE_DIR}/bridge.log 2>&1")
    elif args.cmd == "status":
        state = get_bridge_state()
        print(f"Last processed event ID: {state['last_processed_event_id']}")
        print(f"Last run: {state.get('last_run', 'never')}")
        latest = get_latest_roster_event_id()
        print(f"Latest event in DB: {latest}")
        if latest > state["last_processed_event_id"]:
            print(f"⚠ {latest - state['last_processed_event_id']} events pending")
        else:
            print("✓ Caught up")


if __name__ == "__main__":
    main()
