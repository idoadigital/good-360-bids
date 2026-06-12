"""Bridge: sync QuickBeed customers (mirrored in dashboard.db) into the
roster's nonprofits table so the existing orchestrator/queue manager can
schedule them.

Important: we do NOT write `nonprofit_logins` or `payment_cards` rows for
QuickBeed-sourced orgs. Those credentials and card data are fetched on-demand
from the QuickBeed API at purchase time (see /api/internal/org-config below
and the autobuy_v2 patch). The local roster.db row only carries identity,
queue position, cooldown, and the QuickBeed UUID linkage.
"""
from __future__ import annotations

import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

logger = logging.getLogger("quickbeed_roster_sync")

ROSTER_DB = Path(os.environ.get("ROSTER_DB_PATH", "/app/good360_roster/db/roster.db"))


@contextmanager
def roster_conn():
    ROSTER_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(ROSTER_DB), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def ensure_roster_initialized() -> None:
    """Idempotently create roster.db and the QuickBeed linkage column."""
    if not ROSTER_DB.exists():
        # Bootstrap from the roster module's own schema.
        try:
            import sys
            roster_module_path = Path(__file__).parent.parent / "good360_roster"
            sys.path.insert(0, str(roster_module_path))
            import schema as roster_schema  # type: ignore[import-not-found]
            roster_schema.create_database(str(ROSTER_DB))  # uses its own logic if present
            logger.info("roster.db initialized via schema.create_database")
        except Exception:
            # Fallback: run schema.py's CREATE_TABLE statements directly.
            with open(roster_module_path / "schema.py") as f:
                src = f.read()
            with roster_conn() as c:
                # Pull every CREATE TABLE/INDEX statement out of the source.
                import re
                for stmt in re.findall(r"CREATE\s+(?:TABLE|INDEX|TRIGGER|VIEW)[^;]+;", src, re.IGNORECASE | re.DOTALL):
                    try:
                        c.executescript(stmt)
                    except sqlite3.OperationalError as exc:
                        logger.debug(f"schema stmt skipped: {exc}")
                c.commit()
            logger.info("roster.db initialized via fallback statement extraction")

    # Migration: add quickbeed_customer_id if it isn't there yet.
    with roster_conn() as c:
        cols = {row["name"] for row in c.execute("PRAGMA table_info(nonprofits)").fetchall()}
        if "quickbeed_customer_id" not in cols:
            c.execute("ALTER TABLE nonprofits ADD COLUMN quickbeed_customer_id TEXT")
            c.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_nonprofits_quickbeed "
                "ON nonprofits(quickbeed_customer_id) WHERE quickbeed_customer_id IS NOT NULL"
            )
            c.commit()
            logger.info("Added quickbeed_customer_id column + index to nonprofits")
        # Migration: mirror of the operator's manual queue order (dashboard
        # drag-and-drop) so find_next_available_org can honor it.
        if "manual_rank" not in cols:
            c.execute("ALTER TABLE nonprofits ADD COLUMN manual_rank INTEGER")
            c.commit()
            logger.info("Added manual_rank column to nonprofits")


# QuickBeed status → roster nonprofits.status mapping.
# `active` is the only one we keep eligible. Everything else parks the row.
_STATUS_MAP = {
    "active":      "active",
    "paused":      "cooldown",      # use existing 'cooldown' as a temporary skip
    "inactive":    "inactive",
    "suspended":   "inactive",
    "onboarding":  "pending_setup",
}


def sync_to_roster() -> dict:
    """Read dashboard.db.customers (the QuickBeed mirror) and upsert each
    record into roster.db.nonprofits. Returns counts."""
    from db import get_conn as dashboard_conn

    ensure_roster_initialized()

    with dashboard_conn() as dc:
        customers = dc.execute(
            "SELECT id, organization_name, full_name, email, phone, status, "
            "       max_budget, priority_level, in_rotation, manual_queue_position "
            "FROM customers"
        ).fetchall()

    inserted = updated = 0
    with roster_conn() as rc:
        for cust in customers:
            roster_status = _STATUS_MAP.get(cust["status"], "pending_setup")
            existing = rc.execute(
                "SELECT id FROM nonprofits WHERE quickbeed_customer_id = ?",
                (cust["id"],),
            ).fetchone()

            if existing:
                rc.execute(
                    """UPDATE nonprofits SET
                         org_name = ?,
                         contact_name = ?,
                         contact_email = ?,
                         contact_phone = ?,
                         status = CASE
                            WHEN status = 'cooldown' AND ? = 'active' THEN 'cooldown'
                            ELSE ?
                         END,
                         max_price_override = ?,
                         auto_buy_global = CASE WHEN ? = 'active' AND ? = 1 THEN 1 ELSE 0 END,
                         manual_rank = ?,
                         updated_at = datetime('now')
                       WHERE quickbeed_customer_id = ?""",
                    (
                        cust["organization_name"] or "(unnamed)",
                        cust["full_name"] or "—",
                        cust["email"] or "—",
                        cust["phone"] or "—",
                        roster_status, roster_status,
                        cust["max_budget"],
                        # The operator's dashboard rotation toggle must reach
                        # the purchase engine: QuickBeed-active alone is NOT
                        # eligibility. in_rotation=0 → auto_buy_global=0.
                        roster_status, int(cust["in_rotation"] or 0),
                        # Operator's drag-and-drop order; NULL = unranked
                        # (falls back to LRU behind the ranked set).
                        cust["manual_queue_position"],
                        cust["id"],
                    ),
                )
                updated += 1
            else:
                rc.execute(
                    """INSERT INTO nonprofits
                         (quickbeed_customer_id, org_name, contact_name,
                          contact_email, contact_phone, status,
                          max_price_override, auto_buy_global, manual_rank,
                          subscription_active, agreement_signed)
                       VALUES (?,?,?,?,?,?,?,?,?,1,1)""",
                    (
                        cust["id"],
                        cust["organization_name"] or "(unnamed)",
                        cust["full_name"] or "—",
                        cust["email"] or "—",
                        cust["phone"] or "—",
                        roster_status,
                        cust["max_budget"],
                        1 if (roster_status == "active" and int(cust["in_rotation"] or 0)) else 0,
                        cust["manual_queue_position"],
                    ),
                )
                inserted += 1
        rc.commit()

        # Refresh queue positions for active orgs.
        try:
            rc.execute(
                """WITH ordered AS (
                       SELECT id, ROW_NUMBER() OVER (
                           ORDER BY
                               CASE WHEN last_purchase_date IS NULL THEN 0 ELSE 1 END,
                               COALESCE(last_purchase_date, '9999-12-31') ASC
                       ) AS pos
                       FROM nonprofits WHERE status = 'active'
                   )
                   UPDATE nonprofits
                      SET queue_position = (SELECT pos FROM ordered WHERE ordered.id = nonprofits.id)
                    WHERE status = 'active'"""
            )
            rc.commit()
        except sqlite3.OperationalError:
            # Queue position is best-effort; older schemas without the column
            # just keep their default value.
            pass

    return {"inserted": inserted, "updated": updated, "total_customers": len(customers)}


def get_local_org_id(quickbeed_customer_id: str) -> int | None:
    """Map a QuickBeed UUID to its local nonprofits.id (used by autobuy_v2)."""
    with roster_conn() as c:
        row = c.execute(
            "SELECT id FROM nonprofits WHERE quickbeed_customer_id = ?",
            (quickbeed_customer_id,),
        ).fetchone()
    return int(row["id"]) if row else None


def get_quickbeed_id(local_org_id: int) -> str | None:
    """Reverse: from a local nonprofits.id, get the QuickBeed UUID (or None
    for legacy / non-QuickBeed orgs)."""
    with roster_conn() as c:
        row = c.execute(
            "SELECT quickbeed_customer_id FROM nonprofits WHERE id = ?",
            (local_org_id,),
        ).fetchone()
    return row["quickbeed_customer_id"] if row else None
