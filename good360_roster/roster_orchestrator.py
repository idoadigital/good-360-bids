"""
roster_orchestrator.py — E-Comsetter Good360 Roster System
Main Coordinator — ties vault, queue, notifier, autobuy, billing together
Built: 2026-03-20
"""

import json
import logging
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

# ─── Setup ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "db" / "roster.db"
SIGNAL_FILE = BASE_DIR / ".truck_signal.json"
QUEUE_LOCK = threading.Lock()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)-20s %(levelname)-8s %(message)s",
    handlers=[
        logging.FileHandler(BASE_DIR / "orchestrator.log"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("orchestrator")

# ─── Lazy imports (avoid circular) ──────────────────────────────────────────────
def get_vault():
    from vault import decrypt_field, get_sqlcipher_connection
    return decrypt_field, get_sqlcipher_connection

def get_queue():
    from queue_manager import apply_cooldown, find_next_available_org, get_category_preference
    return find_next_available_org, get_category_preference, apply_cooldown

def get_notifier():
    from notifier import notify_admin_alert, notify_master_card_used, notify_purchase_success, notify_truck_alert
    return notify_truck_alert, notify_purchase_success, notify_master_card_used, notify_admin_alert

def get_billing():
    from billing_manager import record_finding_fee, record_subscription
    return record_finding_fee, record_subscription

def get_autobuy():
    from good360_autobuy_v2 import attempt_purchase, check_master_card_available, verify_org_credentials
    return attempt_purchase, verify_org_credentials, check_master_card_available

# ─── DB helper ─────────────────────────────────────────────────────────────────
def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def log_system_event(event_type: str, severity: str, org_id: int = None,
                     message: str = "", metadata: dict = None):
    conn = get_db_connection()
    try:
        conn.execute(
            """INSERT INTO system_events
               (event_type, severity, nonprofit_id, message, metadata_json)
               VALUES (?, ?, ?, ?, ?)""",
            (event_type, severity, org_id, message, json.dumps(metadata) if metadata else None)
        )
        conn.commit()
    finally:
        conn.close()

def get_config(key: str, default: str = None) -> str:
    conn = get_db_connection()
    try:
        row = conn.execute("SELECT value FROM system_config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default
    finally:
        conn.close()

# ─── Truck Event Logging ────────────────────────────────────────────────────────
def log_truck_event(truck_title: str, truck_url: str, truck_price: float,
                    truck_location: str, truck_category: str,
                    raw_data: dict = None) -> int:
    """Log a newly detected truck and return its event_id."""
    conn = get_db_connection()
    try:
        cursor = conn.execute(
            """INSERT INTO truck_events
               (uuid, detected_at, truck_title, truck_url, truck_price,
                truck_location, truck_category, raw_data_json, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (str(uuid.uuid4()), datetime.utcnow().isoformat(),
             truck_title, truck_url, truck_price, truck_location,
             truck_category, json.dumps(raw_data) if raw_data else None,
             "detected")
        )
        conn.commit()
        event_id = cursor.lastrowid
        logger.info(f"Logged truck event #{event_id}: {truck_title} (${truck_price})")
        return event_id
    finally:
        conn.close()


def update_truck_status(event_id: int, status: str, org_id: int = None,
                        notes: str = None):
    conn = get_db_connection()
    try:
        if org_id:
            conn.execute(
                """UPDATE truck_events
                   SET status=?, assigned_to_org_id=?, assigned_at=?, notes=?
                   WHERE id=?""",
                (status, org_id, datetime.utcnow().isoformat(), notes, event_id)
            )
        else:
            conn.execute("UPDATE truck_events SET status=?, notes=? WHERE id=?",
                        (status, notes, event_id))
        conn.commit()
    finally:
        conn.close()


def get_truck_event(event_id: int) -> sqlite3.Row:
    conn = get_db_connection()
    try:
        return conn.execute("SELECT * FROM truck_events WHERE id=?", (event_id,)).fetchone()
    finally:
        conn.close()

# ─── Core Orchestration Logic ───────────────────────────────────────────────────
def handle_truck_event(event_id: int, auto_run: bool = True) -> dict:
    """
    Main entry point for processing a truck event.
    Returns dict with outcome summary.
    """
    with QUEUE_LOCK:
        truck = get_truck_event(event_id)
        if not truck:
            return {"error": f"Truck event {event_id} not found"}

        if truck["status"] != "detected":
            return {"error": f"Truck event {event_id} already processed (status={truck['status']})"}

        truck_category = truck["truck_category"] or "other"
        truck_price = truck["truck_price"] or 0

        logger.info(f"Processing truck #{event_id}: {truck['truck_title']} [{truck_category}] @ ${truck_price}")
        log_system_event("orchestrator_truck_start", "info", None,
                        f"Processing truck event {event_id}: {truck['truck_title']}")

        # Step 1: Find next available org
        find_next, get_cat_pref, _ = get_queue()
        org = find_next(truck_category, truck_price)
        if not org:
            logger.warning("No available orgs in queue")
            update_truck_status(event_id, "missed", notes="No available orgs in queue")
            return {"status": "no_org_available", "event_id": event_id}

        org_id = org["id"]
        org_name = org["org_name"]
        logger.info(f"Selected org #{org_id}: {org_name} (queue_pos={org['queue_position']})")

        # Step 2: Check category preferences
        cat_pref = get_cat_pref(org_id, truck_category)
        if cat_pref:
            if cat_pref["is_excluded"]:
                logger.info(f"Category {truck_category} is EXCLUDED for org {org_id} — skipping silently")
                update_truck_status(event_id, "missed", org_id,
                                   notes=f"Category {truck_category} excluded for {org_name}")
                return {"status": "category_excluded", "event_id": event_id, "org_id": org_id}

            if not cat_pref["auto_buy_enabled"]:
                # Alert-only mode
                logger.info(f"Org {org_id} alert-only mode for {truck_category}")
                update_truck_status(event_id, "assigned", org_id)
                alert_only_timer(event_id, org_id)
                return {"status": "alert_only", "event_id": event_id, "org_id": org_id}

            # Check price cap
            max_price = (cat_pref["max_price_override"] or
                        org["max_price_override"] or
                        float(get_config("default_max_price", "6400")))
            if truck_price > max_price:
                logger.warning(f"Truck price ${truck_price} exceeds max ${max_price} for org {org_id}")
                update_truck_status(event_id, "missed", org_id,
                                   notes=f"Price ${truck_price} > max ${max_price}")
                return {"status": "price_exceeded", "event_id": event_id, "org_id": org_id}
        else:
            # No category pref = allow all, auto-buy enabled by default
            pass

        # Step 3: Attempt purchase
        if not auto_run:
            return {"status": "would_attempt_purchase", "event_id": event_id, "org_id": org_id}

        update_truck_status(event_id, "assigned", org_id)
        attempt_purchase_fn, _, _ = get_autobuy()
        result = attempt_purchase_fn(org_id, event_id)
        return result


def alert_only_timer(event_id: int, org_id: int):
    """Send alert to org and schedule 30-min follow-up."""
    notify_truck_alert_fn, _, _, notify_admin_alert_fn = get_notifier()
    try:
        notify_truck_alert_fn(org_id, event_id)
        logger.info(f"Alert sent to org {org_id} for truck #{event_id}")
        log_system_event("truck_alert_sent", "info", org_id,
                        f"Alert sent for truck event {event_id}")
    except Exception as e:
        logger.error(f"Failed to send alert for org {org_id}: {e}")
        notify_admin_alert_fn("Alert Failure",
                             f"Failed to send truck alert to org {org_id}: {e}",
                             severity="error")


# ─── Signal Processing (from good360_monitor.py) ───────────────────────────────
def write_signal(event_id: int):
    """Write a signal file for the orchestrator to pick up."""
    with open(SIGNAL_FILE, "w") as f:
        json.dump({"event_id": event_id, "timestamp": datetime.utcnow().isoformat()}, f)
    logger.debug(f"Signal written: event_id={event_id}")


def read_and_clear_signal() -> int | None:
    """Read and clear the signal file. Returns event_id or None."""
    if not SIGNAL_FILE.exists():
        return None
    try:
        with open(SIGNAL_FILE) as f:
            data = json.load(f)
        SIGNAL_FILE.unlink()
        return data.get("event_id")
    except Exception as e:
        logger.error(f"Signal file error: {e}")
        return None


# ─── Daemon Mode ───────────────────────────────────────────────────────────────
def run_daemon(poll_interval: int = 5):
    """Poll for signals and process truck events."""
    import time
    logger.info(f"Orchestrator daemon starting (poll_interval={poll_interval}s)")
    log_system_event("orchestrator_daemon_start", "info", None, "Orchestrator daemon started")

    while True:
        try:
            event_id = read_and_clear_signal()
            if event_id:
                logger.info(f"Daemon received signal for event #{event_id}")
                result = handle_truck_event(event_id)
                logger.info(f"Daemon processed event #{event_id}: {result}")

            # Also check for expired cooldowns periodically
            from queue_manager import check_and_release_expired_cooldowns
            released = check_and_release_expired_cooldowns()
            if released:
                logger.info(f"Released {len(released)} expired cooldowns: {released}")

            time.sleep(poll_interval)
        except KeyboardInterrupt:
            logger.info("Daemon shutting down...")
            break
        except Exception as e:
            logger.error(f"Daemon error: {e}", exc_info=True)
            time.sleep(poll_interval)


# ─── CLI ───────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(prog="roster_orchestrator.py")
    sub = parser.add_subparsers(dest="cmd")

    # Process a truck event
    process = sub.add_parser("process", help="Process a truck event")
    process.add_argument("event_id", type=int, help="Truck event ID")
    process.add_argument("--dry-run", action="store_true", help="Dry run only")

    # Daemon
    sub.add_parser("daemon", help="Run as background daemon")

    # Queue status
    sub.add_parser("queue", help="Show queue status")

    # Manual trigger from external source
    trigger = sub.add_parser("trigger", help="Trigger from external (e.g. from monitor)")
    trigger.add_argument("event_id", type=int, help="Truck event ID")

    # Init signal pipe for monitor
    sub.add_parser("init-signal", help="Initialize signal mechanism")

    args = parser.parse_args()

    if args.cmd == "process":
        result = handle_truck_event(args.event_id, auto_run=not args.dry_run)
        print(json.dumps(result, indent=2, default=str))

    elif args.cmd == "daemon":
        run_daemon()

    elif args.cmd == "queue":
        from queue_manager import get_queue_report
        report = get_queue_report()
        print(f"\n{'#':<4} {'Org Name':<30} {'Status':<12} {'Queue Pos':<10} {'Last Purchase':<20} {'Cooldown Until'}")
        print("-" * 100)
        for r in report:
            print(f"{r['id']:<4} {r['org_name'][:28]:<30} {r['status']:<12} "
                  f"{r.get('queue_position','?'):<10} {str(r.get('last_purchase_date','')):<20} "
                  f"{r.get('cooldown_until','')}")

    elif args.cmd == "trigger":
        # Write signal and process immediately
        write_signal(args.event_id)
        result = handle_truck_event(args.event_id)
        print(json.dumps(result, indent=2, default=str))

    elif args.cmd == "init-signal":
        print(f"Signal file path: {SIGNAL_FILE}")
        print("Add this to good360_monitor.py to trigger orchestrator:")
        print("  import json, subprocess")
        print(f"  signal_file = '{SIGNAL_FILE}'")
        print("  with open(signal_file,'w') as f:")
        print("      json.dump({'event_id': <event_id>, 'timestamp': <timestamp>}, f)")
        print(f"  # Then call: subprocess.Popen(['python', '{__file__}', 'trigger', str(event_id)])")

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
