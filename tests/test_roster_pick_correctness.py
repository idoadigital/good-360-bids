"""Failing-first: the engine must pick the org the operator's roster says,
EVERY time — including when a roster cooldown has expired but was never
swept (the Di'Marco bug, 2026-06-12).

1. find_next_available_org() releases expired cooldowns at the point of
   pick: an org whose status='cooldown' but cooldown_until is in the PAST
   is eligible again, immediately, no background daemon required.
2. A future cooldown is still respected.
3. sync_to_roster() must NOT re-pin an EXPIRED cooldown (it preserved
   status='cooldown' unconditionally, which kept resurrecting the staleness)
   — but a future-dated cooldown survives the sync.

Isolated throwaway container; temp DBs only.
"""
import os
import sqlite3
import sys

failures = []

# ---------------------------------------------------------------------------
# Part 1+2: point-of-pick release in queue_manager
# ---------------------------------------------------------------------------
sys.path.insert(0, "/app/good360_roster")
import queue_manager

qm = str(queue_manager.DB_PATH)
os.makedirs(os.path.dirname(qm), exist_ok=True)
r = sqlite3.connect(qm)
r.executescript("""
CREATE TABLE nonprofits (id INTEGER PRIMARY KEY, org_name TEXT, status TEXT,
  auto_buy_global INTEGER, queue_position INTEGER, cooldown_until TEXT,
  manual_rank INTEGER, last_purchase_date TEXT, updated_at TEXT);
CREATE TABLE system_config (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO system_config VALUES ('cooldown_days','7');
-- StaleCool: top of the operator's queue, cooldown EXPIRED 3 weeks ago,
--            but status still 'cooldown' (the Di'Marco shape)
INSERT INTO nonprofits VALUES (1,'StaleCool','cooldown',1,1,'2026-05-25T22:04:23',0,'2026-05-18T00:00:00',NULL);
-- StillCooling: future cooldown — must NOT be picked
INSERT INTO nonprofits VALUES (2,'StillCooling','cooldown',1,2,'2099-01-01T00:00:00',1,NULL,NULL);
-- Fallback: active, ranked below
INSERT INTO nonprofits VALUES (3,'Fallback','active',1,3,NULL,2,NULL,NULL);
""")
r.commit(); r.close()

pick = queue_manager.find_next_available_org()
if pick is None or pick["org_name"] != "StaleCool":
    failures.append(f"expired-cooldown org not picked: got "
                    f"{pick['org_name'] if pick else None!r} (expected StaleCool)")

r = sqlite3.connect(qm); r.row_factory = sqlite3.Row
row = r.execute("SELECT status, cooldown_until FROM nonprofits WHERE id=1").fetchone()
if row["status"] != "active" or row["cooldown_until"] is not None:
    failures.append(f"expired cooldown not RELEASED at pick time: {dict(row)}")
row2 = r.execute("SELECT status FROM nonprofits WHERE id=2").fetchone()
if row2["status"] != "cooldown":
    failures.append("future cooldown was wrongly released")
r.close()

# pick again with StaleCool removed from rotation -> StillCooling must be
# skipped (future cooldown) -> Fallback
r = sqlite3.connect(qm)
r.execute("UPDATE nonprofits SET auto_buy_global=0 WHERE id=1")
r.commit(); r.close()
pick = queue_manager.find_next_available_org()
if pick is None or pick["org_name"] != "Fallback":
    failures.append(f"future cooldown not respected: got "
                    f"{pick['org_name'] if pick else None!r} (expected Fallback)")

# ---------------------------------------------------------------------------
# Part 3: sync must not re-pin an EXPIRED cooldown
# ---------------------------------------------------------------------------
DASH_DB = os.environ["DASHBOARD_DB"]
d = sqlite3.connect(DASH_DB)
d.executescript("""
CREATE TABLE customers (id TEXT PRIMARY KEY, organization_name TEXT,
  full_name TEXT, email TEXT, phone TEXT, status TEXT, max_budget REAL,
  priority_level INTEGER, in_rotation INTEGER, manual_queue_position INTEGER);
INSERT INTO customers VALUES ('QB-EXP','Expired Co','E','e@x','1','active',NULL,NULL,1,0);
INSERT INTO customers VALUES ('QB-FUT','Future Co','F','f@x','1','active',NULL,NULL,1,1);
""")
d.commit(); d.close()

SYNC_DB = "/tmp/test_roster_sync2.db"
os.environ["ROSTER_DB_PATH"] = SYNC_DB
sys.path.insert(0, "/app/missioncontrol")
import quickbeed_roster_sync as qrs
qrs.ensure_roster_initialized()
rc = sqlite3.connect(SYNC_DB)
rc.execute("INSERT INTO nonprofits (org_name, contact_name, contact_email, contact_phone, "
           "status, cooldown_until, quickbeed_customer_id, subscription_active, agreement_signed) "
           "VALUES ('Expired Co','E','e@x','1','cooldown','2026-05-25T00:00:00','QB-EXP',1,1)")
rc.execute("INSERT INTO nonprofits (org_name, contact_name, contact_email, contact_phone, "
           "status, cooldown_until, quickbeed_customer_id, subscription_active, agreement_signed) "
           "VALUES ('Future Co','F','f@x','1','cooldown','2099-01-01T00:00:00','QB-FUT',1,1)")
rc.commit(); rc.close()

qrs.sync_to_roster()
rc = sqlite3.connect(SYNC_DB); rc.row_factory = sqlite3.Row
exp = rc.execute("SELECT status FROM nonprofits WHERE quickbeed_customer_id='QB-EXP'").fetchone()
fut = rc.execute("SELECT status FROM nonprofits WHERE quickbeed_customer_id='QB-FUT'").fetchone()
if exp["status"] != "active":
    failures.append(f"sync re-pinned an EXPIRED cooldown: status={exp['status']}")
if fut["status"] != "cooldown":
    failures.append(f"sync dropped a FUTURE cooldown: status={fut['status']}")
rc.close()

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: pick releases expired cooldowns, respects future ones; sync can't re-pin stale state")
