"""Failing-first test: the operator's manual roster order must drive the
purchase engine's pick.

1. queue_manager.find_next_available_org() must order by manual_rank first
   (NULLs fall back to LRU queue_position behind the ranked set), with all
   eligibility filters (status, cooldown, auto_buy_global) still applied.
2. quickbeed_roster_sync.sync_to_roster() must propagate
   customers.manual_queue_position -> nonprofits.manual_rank (and migrate
   the column into existing roster DBs).
3. The reorder endpoint must push sync_to_roster() inline so a drag-drop
   takes effect immediately.

Runs in an ISOLATED throwaway container (temp DBs, no volumes).
"""
import os
import re
import sqlite3
import sys

failures = []

# ---------------------------------------------------------------------------
# Part 1: selection ordering (queue_manager at its fixed DB path)
# ---------------------------------------------------------------------------
sys.path.insert(0, "/app/good360_roster")
import queue_manager  # noqa: E402

qm_path = str(queue_manager.DB_PATH)
os.makedirs(os.path.dirname(qm_path), exist_ok=True)
r = sqlite3.connect(qm_path)
r.execute("""CREATE TABLE nonprofits (
    id INTEGER PRIMARY KEY, org_name TEXT, status TEXT,
    auto_buy_global INTEGER, queue_position INTEGER,
    cooldown_until TEXT, manual_rank INTEGER)""")
r.execute("CREATE TABLE system_config (key TEXT PRIMARY KEY, value TEXT)")
r.execute("INSERT INTO system_config VALUES ('cooldown_days','7')")
rows = [
    # id, name,            status,     autobuy, qpos, cooldown, manual_rank
    (1, "LRU-first",       "active",   1,       1,    None,     None),  # oldest LRU
    (2, "ManualSecond",    "active",   1,       2,    None,     1),
    (3, "ManualFirst",     "active",   1,       5,    None,     0),     # worst LRU, top manual
    (4, "ManualButCooling","cooldown", 1,       3,    None,     None),
    (5, "ManualButOff",    "active",   0,       4,    None,     None),
]
r.executemany("INSERT INTO nonprofits VALUES (?,?,?,?,?,?,?)", rows)
r.commit()
r.close()

pick = queue_manager.find_next_available_org()
if pick is None or pick["org_name"] != "ManualFirst":
    failures.append(f"pick #1: expected 'ManualFirst' (manual_rank=0), got "
                    f"{pick['org_name'] if pick else None!r} — manual order ignored")

# Put ManualFirst into cooldown -> next must be ManualSecond, NOT the LRU org
r = sqlite3.connect(qm_path)
r.execute("UPDATE nonprofits SET status='cooldown' WHERE id=3")
r.commit(); r.close()
pick = queue_manager.find_next_available_org()
if pick is None or pick["org_name"] != "ManualSecond":
    failures.append(f"pick #2: expected 'ManualSecond' (manual_rank=1), got "
                    f"{pick['org_name'] if pick else None!r}")

# All ranked orgs ineligible -> unranked falls back to LRU order
r = sqlite3.connect(qm_path)
r.execute("UPDATE nonprofits SET status='cooldown' WHERE id=2")
r.commit(); r.close()
pick = queue_manager.find_next_available_org()
if pick is None or pick["org_name"] != "LRU-first":
    failures.append(f"pick #3: expected LRU fallback 'LRU-first', got "
                    f"{pick['org_name'] if pick else None!r}")

# ---------------------------------------------------------------------------
# Part 2: sync propagates the manual order + migrates the column
# ---------------------------------------------------------------------------
DASH_DB = os.environ["DASHBOARD_DB"]
dash = sqlite3.connect(DASH_DB)
dash.execute("""CREATE TABLE customers (
    id TEXT PRIMARY KEY, organization_name TEXT, full_name TEXT, email TEXT,
    phone TEXT, status TEXT, max_budget REAL, priority_level INTEGER,
    in_rotation INTEGER, manual_queue_position INTEGER)""")
dash.execute("INSERT INTO customers VALUES "
             "('QB-A','Org A','A','a@x.com','1','active',NULL,NULL,1,2)")
dash.execute("INSERT INTO customers VALUES "
             "('QB-B','Org B','B','b@x.com','1','active',NULL,NULL,1,0)")
dash.execute("INSERT INTO customers VALUES "
             "('QB-C','Org C','C','c@x.com','1','active',NULL,NULL,0,NULL)")
dash.commit()
dash.close()

# Point the sync at a FRESH roster db lacking manual_rank, to prove migration.
SYNC_ROSTER = "/tmp/test_roster_sync.db"
os.environ["ROSTER_DB_PATH"] = SYNC_ROSTER
sys.path.insert(0, "/app/missioncontrol")
import quickbeed_roster_sync as qrs  # noqa: E402
qrs.sync_to_roster()

rc = sqlite3.connect(SYNC_ROSTER)
rc.row_factory = sqlite3.Row
cols = {row["name"] for row in rc.execute("PRAGMA table_info(nonprofits)").fetchall()}
if "manual_rank" not in cols:
    failures.append("sync did not migrate manual_rank column into roster.db")
else:
    got = {row["quickbeed_customer_id"]: row["manual_rank"]
           for row in rc.execute(
               "SELECT quickbeed_customer_id, manual_rank FROM nonprofits")}
    if got.get("QB-A") != 2 or got.get("QB-B") != 0 or got.get("QB-C") is not None:
        failures.append(f"manual_rank not propagated correctly: {got}")
rc.close()

# ---------------------------------------------------------------------------
# Part 3: reorder endpoint pushes the sync inline
# ---------------------------------------------------------------------------
src = open("/app/missioncontrol/admin_routes.py").read()
m = re.search(r"def roster_queue_reorder\(.*?\n(?=@bp\.route|\Z)", src, re.DOTALL)
if not m or "sync_to_roster" not in m.group(0):
    failures.append("roster_queue_reorder does not push sync_to_roster() — "
                    "a drag-drop would wait up to 5 min to reach the engine")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: manual roster order drives selection, syncs through, applies immediately")
