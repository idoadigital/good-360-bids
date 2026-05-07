"""
billing_manager.py — E-Comsetter Good360 Roster System
Billing + Fee Recording + Invoice Management
Built: 2026-03-20
"""

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

logger = logging.getLogger("billing")
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

# ─── Config Getters ──────────────────────────────────────────────────────────
def get_finding_fee() -> float:
    return float(get_config("finding_fee_usd", "500"))

def get_subscription_fee() -> float:
    return float(get_config("subscription_fee_usd", "200"))

# ─── Finding Fee ─────────────────────────────────────────────────────────────
def record_finding_fee(org_id: int, purchase_attempt_id: int,
                       truck_price: float) -> int:
    """Record a $500 finding fee for a successful truck find."""
    amount = get_finding_fee()
    due_date = date.today().strftime("%Y-%m-%d")

    with get_db_connection() as conn:
        # Check if already recorded
        existing = conn.execute(
            """SELECT 1 FROM billing_records
               WHERE nonprofit_id=? AND purchase_attempt_id=? AND billing_type='finding_fee'""",
            (org_id, purchase_attempt_id)
        ).fetchone()

        if existing:
            logger.debug(f"Finding fee already recorded for org={org_id} attempt={purchase_attempt_id}")
            return 0

        cursor = conn.execute(
            """INSERT INTO billing_records
               (nonprofit_id, billing_type, amount, currency,
                billing_date, due_date, status, purchase_attempt_id)
               VALUES (?, 'finding_fee', ?, 'USD', date('now'), ?, 'pending', ?)""",
            (org_id, amount, due_date, purchase_attempt_id)
        )
        conn.commit()
        billing_id = cursor.lastrowid

    logger.info(f"Finding fee recorded: org={org_id} amount=${amount}")
    return billing_id

# ─── Subscription Billing ──────────────────────────────────────────────────────
def record_subscription(org_id: int, period_start: str = None,
                        period_end: str = None) -> int:
    """Record monthly subscription fee."""
    amount = get_subscription_fee()
    if not period_start:
        period_start = date.today().replace(day=1).strftime("%Y-%m-%d")
    if not period_end:
        # Next month
        y, m = date.today().year, date.today().month
        from calendar import monthrange
        last_day = monthrange(y, m)[1]
        period_end = f"{y}-{m:02d}-{last_day:02d}"

    due_date = period_start

    with get_db_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO billing_records
               (nonprofit_id, billing_type, amount, currency,
                billing_date, due_date, status)
               VALUES (?, 'subscription', ?, 'USD', ?, ?, 'pending')""",
            (org_id, amount, period_start, due_date)
        )
        conn.commit()
        return cursor.lastrowid

def get_org_balance(org_id: int) -> dict[str, float]:
    """Get outstanding balance for an org."""
    with get_db_connection() as conn:
        row = conn.execute(
            """SELECT
                   SUM(CASE WHEN status='pending' THEN amount ELSE 0 END) as pending,
                   SUM(CASE WHEN status='overdue' THEN amount ELSE 0 END) as overdue,
                   SUM(CASE WHEN status='paid' THEN amount ELSE 0 END) as paid,
                   SUM(amount) as total
               FROM billing_records WHERE nonprofit_id=?""",
            (org_id,)
        ).fetchone()
    return {
        "pending": row["pending"] or 0.0,
        "overdue": row["overdue"] or 0.0,
        "paid": row["paid"] or 0.0,
        "total": row["total"] or 0.0,
    }

def mark_paid(billing_id: int):
    with get_db_connection() as conn:
        conn.execute(
            """UPDATE billing_records
               SET status='paid', paid_date=date('now')
               WHERE id=?""",
            (billing_id,)
        )
        conn.commit()
    logger.info(f"Billing record {billing_id} marked paid")

def mark_overdue(billing_id: int):
    with get_db_connection() as conn:
        conn.execute("UPDATE billing_records SET status='overdue' WHERE id=?", (billing_id,))
        conn.commit()
    logger.info(f"Billing record {billing_id} marked overdue")

def get_pending_invoices(limit: int = 50) -> list[dict]:
    with get_db_connection() as conn:
        rows = conn.execute(
            """SELECT br.*, n.org_name
               FROM billing_records br
               JOIN nonprofits n ON n.id = br.nonprofit_id
               WHERE br.status IN ('pending','overdue')
               ORDER BY br.due_date ASC LIMIT ?""",
            (limit,)
        ).fetchall()
    return [dict(r) for r in rows]

def generate_invoice_number(billing_id: int) -> str:
    return f"INV-{date.today().strftime('%Y%m%d')}-{billing_id:04d}"

# ─── CLI ─────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(prog="billing_manager.py")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("pending", help="Show pending invoices")
    bal = sub.add_parser("balance", help="Show org balance")
    bal.add_argument("org_id", type=int)
    args = parser.parse_args()

    if args.cmd == "pending":
        rows = get_pending_invoices()
        print(f"\n{'ID':>4} {'Org':<25} {'Type':<15} {'Amount':>10} {'Due':<12} {'Status':<10}")
        print("-" * 80)
        for r in rows:
            print(f"{r['id']:>4} {r['org_name'][:25]:<25} {r['billing_type']:<15} "
                  f"${r['amount']:>8.2f} {r['due_date']:<12} {r['status']:<10}")
    elif args.cmd == "balance":
        b = get_org_balance(args.org_id)
        print(f"\nOrg ID={args.org_id} Billing Summary:")
        print(f"  Paid:    ${b['paid']:,.2f}")
        print(f"  Pending: ${b['pending']:,.2f}")
        print(f"  Overdue: ${b['overdue']:,.2f}")
        print(f"  Total:   ${b['total']:,.2f}")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
