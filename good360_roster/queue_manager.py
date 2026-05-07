
"""
queue_manager.py — E-Comsetter Good360 Roster System
7-Day Cooldown Rotation Queue Manager
Built: 2026-03-20
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger("queue_manager")
DB_PATH = Path(__file__).parent / "db" / "roster.db"


@contextmanager
def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def get_config(key: str, default: str = None) -> str:
    with get_db_connection() as conn:
        row = conn.execute("SELECT value FROM system_config WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def get_cooldown_days() -> int:
    return int(get_config("cooldown_days", "7"))


def get_default_max_price() -> float:
    return float(get_config("default_max_price", "6400"))


# —— Queue Position Management ——

def assign_queue_positions():
    """Assigns queue_position to all active orgs based on last_purchase_date.
    Org that bought longest ago = position 1. Org never bought = position 0 (first in line).
    """
    with get_db_connection() as conn:
        conn.execute("""
            WITH ordered AS (
                SELECT
                    id,
                    last_purchase_date,
                    ROW_NUMBER() OVER (
                        ORDER BY
                            CASE WHEN last_purchase_date IS NULL THEN 0 ELSE 1 END,
                            COALESCE(last_purchase_date, '9999-12-31') ASC
                    ) AS new_position
                FROM nonprofits
                WHERE status = 'active'
            )
            UPDATE nonprofits
            SET queue_position = (SELECT new_position FROM ordered WHERE ordered.id = nonprofits.id)
            WHERE status = 'active'
        """)
        conn.commit()
    logger.info("Queue positions assigned")


def get_queue_position(org_id: int) -> int:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT queue_position FROM nonprofits WHERE id = ?", (org_id,)
        ).fetchone()
        return row[0] if row else 0


def find_next_available_org(truck_category: str = None, truck_price: float = None) -> sqlite3.Row | None:
    """Find the next org in the queue that is active (not in cooldown).
    Sorts by queue_position ASC (position 1 = longest waiting).
    Returns the org Row or None.
    """
    cooldown_days = get_cooldown_days()
    with get_db_connection() as conn:
        now = datetime.utcnow().isoformat()
        cutoff = (datetime.utcnow() - timedelta(days=cooldown_days)).isoformat()
        row = conn.execute("""
            SELECT n.* FROM nonprofits n
            WHERE n.status = 'active'
              AND (n.cooldown_until IS NULL OR n.cooldown_until < ?)
              AND n.auto_buy_global = 1
            ORDER BY n.queue_position ASC
            LIMIT 1
        """, (now,)).fetchone()
        return row


def get_category_preference(org_id: int, category_key: str) -> sqlite3.Row | None:
    """Get category preference for an org. Returns None if no pref set."""
    with get_db_connection() as conn:
        return conn.execute(
            "SELECT * FROM nonprofit_category_preferences WHERE nonprofit_id = ? AND category_key = ?",
            (org_id, category_key)
        ).fetchone()


def apply_cooldown(org_id: int, days: int = None) -> str:
    """Set 7-day cooldown for org after successful purchase. Returns new cooldown_until."""
    if days is None:
        days = get_cooldown_days()
    cooldown_until = (datetime.utcnow() + timedelta(days=days)).isoformat()
    with get_db_connection() as conn:
        conn.execute("""
            UPDATE nonprofits
            SET status = 'cooldown',
                cooldown_until = ?,
                last_purchase_date = ?,
                total_trucks_found = total_trucks_found + 1,
                updated_at = ?
            WHERE id = ?
        """, (cooldown_until, datetime.utcnow().isoformat(), datetime.utcnow().isoformat(), org_id))
        conn.commit()
    assign_queue_positions()
    logger.info(f"Cooldown applied for org {org_id} until {cooldown_until}")
    log_system_event("cooldown_applied", "info", org_id,
                    f"Cooldown applied for {days} days, until {cooldown_until}")
    return cooldown_until


def release_cooldown(org_id: int):
    """Manually release cooldown (admin function)."""
    with get_db_connection() as conn:
        conn.execute("""
            UPDATE nonprofits
            SET status = 'active', cooldown_until = NULL, updated_at = ?
            WHERE id = ?
        """, (datetime.utcnow().isoformat(), org_id))
        conn.commit()
    assign_queue_positions()
    logger.info(f"Cooldown released for org {org_id}")
    log_system_event("cooldown_released", "info", org_id, "Cooldown manually released")


def check_and_release_expired_cooldowns() -> list[int]:
    """Check for expired cooldowns and release them. Returns list of released org IDs."""
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT id FROM nonprofits
            WHERE status = 'cooldown' AND cooldown_until < ?
        """, (datetime.utcnow().isoformat(),)).fetchall()
        released_ids = [r[0] for r in rows]
        if released_ids:
            conn.execute("""
                UPDATE nonprofits
                SET status = 'active', cooldown_until = NULL, updated_at = ?
                WHERE status = 'cooldown' AND cooldown_until < ?
            """, (datetime.utcnow().isoformat(), datetime.utcnow().isoformat()))
            conn.commit()
            assign_queue_positions()
    if released_ids:
        logger.info(f"Released {len(released_ids)} expired cooldowns: {released_ids}")
    return released_ids


def on_purchase_success(org_id: int, truck_event_id: int, confirmation_number: str = None) -> str:
    """Called by autobuy after successful purchase. Sets cooldown + updates truck event."""
    cooldown_until = apply_cooldown(org_id)
    with get_db_connection() as conn:
        conn.execute("""
            UPDATE truck_events
            SET status = 'purchased', assigned_at = ?, notes = ?
            WHERE id = ?
        """, (datetime.utcnow().isoformat(),
               f"Confirmed: {confirmation_number}" if confirmation_number else None,
               truck_event_id))
        conn.commit()
    return cooldown_until


def get_queue_report() -> list[dict]:
    """Get full queue report with all org positions."""
    with get_db_connection() as conn:
        rows = conn.execute("""
            SELECT
                n.id, n.org_name, n.status, n.queue_position,
                n.last_purchase_date, n.cooldown_until, n.total_trucks_found,
                n.auto_buy_global, n.subscription_active
            FROM nonprofits n
            WHERE n.status IN ('active', 'cooldown')
            ORDER BY n.queue_position ASC
        """).fetchall()
        return [dict(r) for r in rows]


def get_org_queue_position(org_id: int) -> int:
    return get_queue_position(org_id)


def log_system_event(event_type: str, severity: str, org_id: int = None,
                     message: str = "", metadata: dict = None):
    import json
    with get_db_connection() as conn:
        conn.execute("""
            INSERT INTO system_events (event_type, severity, nonprofit_id, message, metadata_json)
            VALUES (?, ?, ?, ?, ?)
        """, (event_type, severity, org_id, message, json.dumps(metadata) if metadata else None))
        conn.commit()


# —— CLI ——
def main():
    import argparse
    parser = argparse.ArgumentParser(prog="queue_manager.py")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("assign", help="Reassign queue positions")
    sub.add_parser("report", help="Show queue report")
    sub.add_parser("release-expired", help="Release expired cooldowns")
    peek = sub.add_parser("peek", help="Peek at next N orgs")
    peek.add_argument("--limit", "-n", type=int, default=5)
    status_p = sub.add_parser("status", help="Org status")
    status_p.add_argument("org_id", type=int)
    release_p = sub.add_parser("release", help="Manually release cooldown")
    release_p.add_argument("org_id", type=int)
    args = parser.parse_args()

    if args.cmd == "assign":
        assign_queue_positions()
        print("Queue positions assigned.")
    elif args.cmd == "report" or not args.cmd:
        report = get_queue_report()
        print(f"\n{'#':<4} {'Org Name':<30} {'Status':<12} {'Pos':<5} {'Last Purchase':<20} {'Cooldown Until':<27} {'Trucks'}")
        print("-" * 110)
        for r in report:
            lp = str(r.get('last_purchase_date') or 'NEVER')[:19]
            cu = str(r.get('cooldown_until') or '')
            print(f"{r['id']:<4} {r['org_name'][:28]:<30} {r['status']:<12} "
                  f"{(r.get('queue_position') or 0):<5} {lp:<20} {cu[:26]:<27} {r.get('total_trucks_found',0):>6}")
    elif args.cmd == "peek":
        with get_db_connection() as conn:
            rows = conn.execute("""
                SELECT n.id, n.queue_position, n.org_name, n.status,
                       n.cooldown_until, n.auto_buy_global
                FROM nonprofits n
                WHERE n.status = 'active'
                ORDER BY n.queue_position ASC LIMIT ?""",
                (args.limit,)
            ).fetchall()
            for r in rows:
                print(f"  [{r['queue_position']}] {r['org_name']} | status={r['status']} "
                      f"| auto_buy={r['auto_buy_global']}")
    elif args.cmd == "status":
        with get_db_connection() as conn:
            row = conn.execute("SELECT * FROM nonprofits WHERE id = ?",
                               (args.org_id,)).fetchone()
            if row:
                for k in row.keys():
                    print(f"  {k}: {row[k]}")
            else:
                print(f"Org {args.org_id} not found.")
    elif args.cmd == "release":
        release_cooldown(args.org_id)
        assign_queue_positions()
        print(f"Cooldown released for org {args.org_id}.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
