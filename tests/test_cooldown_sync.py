"""Issue 1 regression test: a roster cooldown must be written back to the
dashboard's customers table, or the monitor/UI keep showing the customer as
available after a live purchase.

Runs in an ISOLATED throwaway container: temp dashboard db via DASHBOARD_DB
env, temp roster db at queue_manager's fixed path (no volumes mounted, so
nothing here can touch production data).
"""
import os
import sqlite3
import sys

DASH_DB = os.environ["DASHBOARD_DB"]          # e.g. /tmp/test_dashboard.db
ISO = "2026-06-19T01:00:00.123456"            # roster-style ISO timestamp
EXPECTED_DASH = "2026-06-19 01:00:00"         # dashboard/sqlite format

# --- fixture: minimal dashboard db ----------------------------------------
dash = sqlite3.connect(DASH_DB)
dash.execute("""CREATE TABLE customers (
    id TEXT PRIMARY KEY, organization_name TEXT, status TEXT,
    in_rotation INTEGER, cooldown_until TEXT,
    last_used_at TEXT, last_purchase_at TEXT)""")
dash.execute(
    "INSERT INTO customers VALUES ('QB123','Test Org','active',1,NULL,NULL,NULL)")
dash.commit()
dash.close()

# --- fixture: minimal roster db at the module's fixed path ----------------
sys.path.insert(0, "/app/good360_roster")
import queue_manager  # noqa: E402

roster_path = str(queue_manager.DB_PATH)
os.makedirs(os.path.dirname(roster_path), exist_ok=True)
r = sqlite3.connect(roster_path)
r.execute("""CREATE TABLE IF NOT EXISTS nonprofits (
    id INTEGER PRIMARY KEY, org_name TEXT, quickbeed_customer_id TEXT)""")
r.execute("INSERT INTO nonprofits (id, org_name, quickbeed_customer_id) "
          "VALUES (1, 'Test Org', 'QB123')")
r.commit()
r.close()

# --- the tests --------------------------------------------------------------
import good360_autobuy_v2 as ab  # noqa: E402

failures = []

if not hasattr(ab, "_sync_cooldown_to_dashboard"):
    failures.append("helper _sync_cooldown_to_dashboard does not exist")
else:
    # 1. happy path: write-back lands in the dashboard, in its date format
    ab._sync_cooldown_to_dashboard(1, ISO)
    d = sqlite3.connect(DASH_DB)
    d.row_factory = sqlite3.Row
    row = d.execute("SELECT * FROM customers WHERE id='QB123'").fetchone()
    if row["cooldown_until"] != EXPECTED_DASH:
        failures.append(f"cooldown_until = {row['cooldown_until']!r}, "
                        f"expected {EXPECTED_DASH!r}")
    if not row["last_purchase_at"]:
        failures.append("last_purchase_at not set")
    # 2. the monitor's own eligibility predicate must now exclude them
    elig = d.execute(
        "SELECT 1 FROM customers WHERE id='QB123' AND "
        "(cooldown_until IS NULL OR cooldown_until < datetime('now'))"
    ).fetchone()
    if elig:
        failures.append("customer still eligible per the monitor's cooldown check")
    d.close()
    # 3. unknown org: must not raise, must not write anything
    try:
        ab._sync_cooldown_to_dashboard(999, ISO)
    except Exception as e:  # noqa: BLE001
        failures.append(f"helper raised on unknown org: {e}")

# 4. wiring: the success path of attempt_purchase must call the helper
import inspect  # noqa: E402
src = inspect.getsource(ab.attempt_purchase)
if "_sync_cooldown_to_dashboard" not in src:
    failures.append("attempt_purchase success path does not call "
                    "_sync_cooldown_to_dashboard")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: cooldown write-back works and is wired into the success path")
