"""Failing-first tests for the dynamic buyer history (spec 2026-06-12).

1. Migration: purchase_attempts gains order_status / order_status_source /
   order_status_updated_at / proof.
2. order_verifier.sync_rows(): matches confirmation_number to Order History
   rows, sets status+total+proof — but NEVER overwrites a manual status.
3. admin_routes._set_order_status(): operator buttons set manual status,
   reject unknown statuses.
4. autobuy_v2 seeds proof + order_status='approved' on a live success.

Isolated throwaway container; temp DBs only.
"""
import json
import os
import sqlite3
import sys

os.environ["ROSTER_DB_PATH"] = "/tmp/test_roster.db"
os.environ["DASHBOARD_DB"] = "/tmp/test_dash.db"

failures = []

# --- fixture roster db (pre-migration shape) --------------------------------
r = sqlite3.connect("/tmp/test_roster.db")
r.executescript("""
CREATE TABLE purchase_attempts (id INTEGER PRIMARY KEY, truck_event_id INT,
  nonprofit_id INT, started_at TEXT, completed_at TEXT, status TEXT, mode TEXT,
  attempt_number INT, error_message TEXT, screenshot_path TEXT,
  confirmation_number TEXT, order_total REAL, cooldown_applied INT);
CREATE TABLE truck_events (id INTEGER PRIMARY KEY, truck_title TEXT, truck_url TEXT, truck_price REAL);
CREATE TABLE nonprofits (id INTEGER PRIMARY KEY, org_name TEXT, contact_email TEXT,
  quickbeed_customer_id TEXT, manual_rank INTEGER);
INSERT INTO nonprofits (id, org_name, quickbeed_customer_id) VALUES (6,'Org','QB1');
INSERT INTO truck_events VALUES (57,'Truck','http://x',0);
INSERT INTO purchase_attempts (id, truck_event_id, nonprofit_id, started_at, status, confirmation_number)
  VALUES (36, 57, 6, datetime('now'), 'success', '100272562'),
         (37, 57, 6, datetime('now'), 'success', '100272563');
""")
r.commit(); r.close()

# --- 1. migration ------------------------------------------------------------
sys.path.insert(0, "/app/missioncontrol")
import quickbeed_roster_sync as qrs
qrs.ensure_roster_initialized()
r = sqlite3.connect("/tmp/test_roster.db")
cols = {row[1] for row in r.execute("PRAGMA table_info(purchase_attempts)")}
need = {"order_status", "order_status_source", "order_status_updated_at", "proof"}
if not need.issubset(cols):
    failures.append(f"migration missing columns: {need - cols}")
r.close()

# --- 2. verifier sync logic --------------------------------------------------
try:
    import order_verifier as ov
except ImportError:
    failures.append("missioncontrol/order_verifier.py does not exist")
    ov = None

if ov and not failures:
    # pre-set attempt 37 to a MANUAL canceled — sync must not touch it
    r = sqlite3.connect("/tmp/test_roster.db")
    r.execute("UPDATE purchase_attempts SET order_status='canceled', "
              "order_status_source='manual' WHERE id=37")
    r.commit(); r.close()

    site_rows = [
        {"order_id": "100272562", "status": "Approved", "admin_fee": 5837.12,
         "date": "06/12/2026"},
        {"order_id": "100272563", "status": "Approved", "admin_fee": 1000.00,
         "date": "06/12/2026"},
        {"order_id": "999999999", "status": "Approved", "admin_fee": 1.00,
         "date": "06/12/2026"},  # no matching attempt — ignored
    ]
    n = ov.sync_rows(org_id=6, site_rows=site_rows,
                     verify_screenshot="/tmp/fake.png")
    r = sqlite3.connect("/tmp/test_roster.db"); r.row_factory = sqlite3.Row
    a36 = r.execute("SELECT * FROM purchase_attempts WHERE id=36").fetchone()
    a37 = r.execute("SELECT * FROM purchase_attempts WHERE id=37").fetchone()
    if a36["order_status"] != "approved" or a36["order_status_source"] != "auto":
        failures.append(f"sync did not set auto status: {dict(a36)}")
    if a36["order_total"] != 5837.12:
        failures.append(f"sync did not set order_total: {a36['order_total']}")
    proof = json.loads(a36["proof"] or "{}")
    if not proof.get("verifications"):
        failures.append("sync did not append a verification proof entry")
    if a37["order_status"] != "canceled" or a37["order_status_source"] != "manual":
        failures.append("sync OVERWROTE a manual status — manual must win")
    if n != 1:
        failures.append(f"sync_rows returned {n}, expected 1 updated row")
    r.close()

# --- 3. manual status setter --------------------------------------------------
import admin_routes
if not hasattr(admin_routes, "_set_order_status"):
    failures.append("admin_routes._set_order_status does not exist")
else:
    ok, err = admin_routes._set_order_status(36, "delivered")
    r = sqlite3.connect("/tmp/test_roster.db"); r.row_factory = sqlite3.Row
    a36 = r.execute("SELECT * FROM purchase_attempts WHERE id=36").fetchone()
    if not ok or a36["order_status"] != "delivered" or a36["order_status_source"] != "manual":
        failures.append(f"manual set failed: ok={ok} err={err} row={dict(a36)}")
    r.close()
    ok, err = admin_routes._set_order_status(36, "exploded")
    if ok:
        failures.append("unknown status was accepted")
    ok, err = admin_routes._set_order_status(424242, "delivered")
    if ok:
        failures.append("nonexistent attempt was accepted")

# --- 4. autobuy seeds proof on success ----------------------------------------
sys.path.insert(0, "/app/good360_roster")
import inspect
import good360_autobuy_v2 as ab
if not hasattr(ab, "_seed_purchase_proof"):
    failures.append("autobuy_v2._seed_purchase_proof does not exist")
elif "_seed_purchase_proof" not in inspect.getsource(ab.attempt_purchase):
    failures.append("attempt_purchase success path does not seed proof")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: lifecycle migration, sync logic, manual setter, proof seeding")

# --- 5. monitor wires the daily verifier --------------------------------------
import good360_monitor as mon
if not hasattr(mon, "_maybe_run_order_verifier"):
    print("FAIL:\n  - monitor _maybe_run_order_verifier does not exist")
    sys.exit(1)
if "_maybe_run_order_verifier" not in inspect.getsource(mon.main):
    print("FAIL:\n  - main() does not call _maybe_run_order_verifier")
    sys.exit(1)
print("PASS: monitor daily-verifier wiring present")
