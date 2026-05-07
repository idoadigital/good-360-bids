"""
health_checker.py — E-Comsetter Good360 Roster System
Daily Credential + Account Health Verification
Built: 2026-03-20
"""

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("health_checker")
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

def log_system_event(event_type: str, severity: str, org_id: int = None,
                     message: str = "", metadata: dict = None):
    import json
    with get_db_connection() as conn:
        conn.execute(
            """INSERT INTO system_events
               (event_type, severity, nonprofit_id, message, metadata_json)
               VALUES (?, ?, ?, ?, ?)""",
            (event_type, severity, org_id, message, json.dumps(metadata) if metadata else None)
        )
        conn.commit()

# ─── Health Check: Credentials ───────────────────────────────────────────────
def check_org_credentials(org_id: int) -> dict:
    """
    Check org credentials by attempting a test login.
    Returns dict with keys: success, login_verified, last_login, error
    """
    try:
        from good360_autobuy_v2 import verify_org_credentials
        success, msg = verify_org_credentials(org_id)
        return {"success": success, "error": msg}
    except Exception as e:
        logger.error(f"Credential check failed for org {org_id}: {e}")
        return {"success": False, "error": str(e)}


def check_org_payment_methods(org_id: int) -> dict:
    """
    Check payment methods health: active count, recent decline rates.
    """
    with get_db_connection() as conn:
        cards = conn.execute(
            """SELECT priority, is_active, decline_count,
                      last_declined, last_used
               FROM nonprofit_payment_methods
               WHERE nonprofit_id = ?""",
            (org_id,)
        ).fetchall()

    if not cards:
        return {"status": "critical", "message": "No payment methods", "cards": []}

    card_health = []
    for c in cards:
        health = "ok"
        if c["decline_count"] >= 3:
            health = "warning"
        if c["decline_count"] >= 5:
            health = "critical"
        card_health.append({
            "priority": c["priority"],
            "is_active": bool(c["is_active"]),
            "decline_count": c["decline_count"],
            "last_declined": c["last_declined"],
            "health": health,
        })

    worst = max(c["health"] for c in card_health)
    return {"status": worst, "cards": card_health}


def check_org_subscription(org_id: int) -> dict:
    """Check if org has active subscription and no overdue invoices."""
    with get_db_connection() as conn:
        org = conn.execute("SELECT subscription_active FROM nonprofits WHERE id=?", (org_id,)).fetchone()
        overdue = conn.execute(
            """SELECT COUNT(*) FROM billing_records
               WHERE nonprofit_id=? AND status='overdue'""", (org_id,)
        ).fetchone()[0]
        pending_count = conn.execute(
            """SELECT COUNT(*) FROM billing_records
               WHERE nonprofit_id=? AND status='pending'""", (org_id,)
        ).fetchone()[0]

    if not org or not org["subscription_active"]:
        status = "critical"
    elif overdue > 0:
        status = "critical"
    elif pending_count > 2:
        status = "warning"
    else:
        status = "ok"

    return {"status": status, "subscription_active": bool(org["subscription_active"] if org else False),
            "overdue_invoices": overdue, "pending_invoices": pending_count}


def check_org_category_setup(org_id: int) -> dict:
    """Check if org has category preferences set."""
    with get_db_connection() as conn:
        cats = conn.execute(
            """SELECT COUNT(*) as cnt,
                      SUM(auto_buy_enabled) as auto_count,
                      SUM(is_excluded) as excl_count
               FROM nonprofit_category_preferences WHERE nonprofit_id=?""",
            (org_id,)
        ).fetchone()

    cnt = cats["cnt"] if cats else 0
    if cnt == 0:
        status = "warning"  # No preferences set — all allowed
    elif cnt < 5:
        status = "info"
    else:
        status = "ok"

    return {"status": status, "categories_configured": cnt}


# ─── Full Health Check ────────────────────────────────────────────────────────
def run_health_check(org_id: int) -> dict:
    """Run full health check for one org."""
    with get_db_connection() as conn:
        org = conn.execute("SELECT * FROM nonprofits WHERE id = ?", (org_id,)).fetchone()

    if not org:
        return {"error": f"Org {org_id} not found"}

    results = {
        "org_id": org_id,
        "org_name": org["org_name"],
        "status": org["status"],
        "credential_check": check_org_credentials(org_id),
        "payment_check": check_org_payment_methods(org_id),
        "subscription_check": check_org_subscription(org_id),
        "category_check": check_org_category_setup(org_id),
    }

    # Overall health score
    statuses = [
        results["credential_check"]["success"],
        results["payment_check"]["status"],
        results["subscription_check"]["status"],
        results["category_check"]["status"],
    ]

    overall = "ok"
    if any(s == "critical" for s in statuses):
        overall = "critical"
    elif any(s == "warning" for s in statuses):
        overall = "warning"
    elif any(s is False for s in statuses):
        overall = "critical"

    results["overall"] = overall
    return results


def run_all_health_checks() -> list[dict]:
    """Run health check for all active orgs."""
    with get_db_connection() as conn:
        orgs = conn.execute(
            "SELECT id FROM nonprofits WHERE status IN ('active','cooldown')"
        ).fetchall()

    results = []
    for org in orgs:
        result = run_health_check(org["id"])
        results.append(result)

    # Print report
    print(f"\n{'Org':<30} {'Overall':<10} {'Creds':<8} {'Payment':<10} {'Subs':<10} {'Cats':<8}")
    print("-" * 80)
    for r in results:
        cred = "✓" if r.get("credential_check", {}).get("success") else "✗"
        pay = r.get("payment_check", {}).get("status", "?")
        sub = r.get("subscription_check", {}).get("status", "?")
        cat = r.get("category_check", {}).get("status", "?")
        overall = r.get("overall", "?")
        flag = "⚠" if overall == "warning" else "🔴" if overall == "critical" else "🟢"
        print(f"{flag} {r['org_name'][:28]:<30} {overall:<10} {cred:<8} {pay:<10} {sub:<10} {cat:<8}")

    # Summary
    total = len(results)
    crit = sum(1 for r in results if r.get("overall") == "critical")
    warn = sum(1 for r in results if r.get("overall") == "warning")
    ok   = sum(1 for r in results if r.get("overall") == "ok")
    print(f"\nSummary: {total} orgs | 🟢OK={ok} | ⚠Warnings={warn} | 🔴Critical={crit}")

    # Log
    log_system_event("health_check_run", "info", None,
                    f"Health check: {total} orgs, {crit} critical, {warn} warnings, {ok} ok")

    return results


def check_cooldown_expirations():
    """Release expired cooldowns and log results."""
    from queue_manager import check_and_release_expired_cooldowns
    released = check_and_release_expired_cooldowns()
    if released:
        print(f"Released {len(released)} expired cooldowns: {released}")
        log_system_event("cooldown_expired", "info", None,
                        f"Released cooldowns for orgs: {released}")
    return released


# ─── CLI ──────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(prog="health_checker.py")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("all", help="Run health check on all active orgs")
    single = sub.add_parser("org", help="Health check for single org")
    single.add_argument("org_id", type=int)
    sub.add_parser("release-cooldowns", help="Check and release expired cooldowns")
    args = parser.parse_args()

    if args.cmd == "all" or not args.cmd:
        run_all_health_checks()
    elif args.cmd == "org":
        import json
        result = run_health_check(args.org_id)
        print(json.dumps(result, indent=2, default=str))
    elif args.cmd == "release-cooldowns":
        released = check_cooldown_expirations()
        print(f"Released: {released}")
    else:
        parser.print_help()

if __name__ == "__main__":
    main()
