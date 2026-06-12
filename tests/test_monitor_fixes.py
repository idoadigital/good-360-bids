"""Failing-first tests for two monitor fixes:

1. _autobuy_banner(): the AUTO-BUY banner must reflect the LIVE
   tracked_products toggles, not a hardcoded product list.
2. mark_customer_assigned(): was dead code; must release the short
   in-flight reservation after an attempt, but must NEVER clear a real
   post-purchase cooldown (which the engine's write-back sets), and must
   actually be called from the roster path in main().

Runs in an ISOLATED throwaway container: temp dashboard db via DASHBOARD_DB
env, no volumes mounted — cannot touch production data.
"""
import inspect
import os
import sqlite3
import sys

DASH_DB = os.environ["DASHBOARD_DB"]

# --- fixtures ---------------------------------------------------------------
dash = sqlite3.connect(DASH_DB)
dash.execute("""CREATE TABLE customers (
    id TEXT PRIMARY KEY, organization_name TEXT, status TEXT, in_rotation INTEGER,
    cooldown_until TEXT, last_used_at TEXT, last_purchase_at TEXT)""")
# A: short in-flight hold; B: real 7-day cooldown; C: no cooldown at all
dash.execute("INSERT INTO customers VALUES "
             "('A','Org A','active',1, datetime('now','+5 minutes'), NULL, NULL)")
dash.execute("INSERT INTO customers VALUES "
             "('B','Org B','active',1, datetime('now','+7 days'), NULL, NULL)")
dash.execute("INSERT INTO customers VALUES ('C','Org C','active',1, NULL, NULL, NULL)")
dash.execute("""CREATE TABLE tracked_products (
    name TEXT PRIMARY KEY, tracked INTEGER, autobuy_enabled INTEGER)""")
dash.execute("INSERT INTO tracked_products VALUES "
             "('Amazon Assorted Softlines Truckload - Maysville, KY', 1, 1)")
dash.execute("INSERT INTO tracked_products VALUES "
             "('Amazon Variety Truckload - Maysville, KY', 1, 1)")
# tracked but autobuy OFF — must NOT appear in the banner
dash.execute("INSERT INTO tracked_products VALUES "
             "('Amazon New Unsorted Truckload - Maysville, KY', 1, 0)")
# autobuy on but NOT tracked — must NOT appear either
dash.execute("INSERT INTO tracked_products VALUES "
             "('Amazon Houseware Truckload - Maysville, KY', 0, 1)")
dash.commit()
dash.close()

import good360_monitor as mon  # noqa: E402

failures = []

# --- 1. dynamic banner ------------------------------------------------------
if not hasattr(mon, "_autobuy_banner"):
    failures.append("_autobuy_banner() does not exist")
else:
    b = mon._autobuy_banner()
    if "Softlines" not in b or "Variety" not in b:
        failures.append(f"banner missing autobuy-enabled products: {b!r}")
    if "New Unsorted" in b or "Houseware" in b:
        failures.append(f"banner lists disabled/untracked products: {b!r}")
    if "6,400" not in b:
        failures.append(f"banner missing the real MAX_AUTO_PAY cap: {b!r}")

main_src = inspect.getsource(mon.main)
if "_autobuy_banner" not in main_src:
    failures.append("main() does not use _autobuy_banner (banner still hardcoded)")
if "New Unsorted + Variety + Houseware" in main_src:
    failures.append("hardcoded banner text still present in main()")

# --- 2. guarded reservation release -----------------------------------------
mon.mark_customer_assigned("A", "FAILED")
mon.mark_customer_assigned("B", "FAILED")
mon.mark_customer_assigned("C", "SUCCESS")
mon.mark_customer_assigned(None, "FAILED")  # must not raise

d = sqlite3.connect(DASH_DB)
d.row_factory = sqlite3.Row
a = d.execute("SELECT cooldown_until FROM customers WHERE id='A'").fetchone()
bb = d.execute("SELECT cooldown_until FROM customers WHERE id='B'").fetchone()
if a["cooldown_until"] is not None:
    failures.append("short in-flight hold was NOT released")
if bb["cooldown_until"] is None:
    failures.append("real 7-day cooldown was WRONGLY cleared "
                    "(would undo the dashboard write-back fix)")
d.close()

# --- 3. wiring: roster path must release the reservation ---------------------
if "mark_customer_assigned" not in main_src:
    failures.append("main() never calls mark_customer_assigned (still dead code)")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: dynamic banner + guarded reservation release, wired into main()")
