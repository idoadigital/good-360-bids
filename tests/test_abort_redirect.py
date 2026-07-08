"""Phase 5 — operator abort of an in-flight autobuy + redirect to a chosen
nonprofit. MONEY-PATH invariants under test:

1. Migration: purchase_attempts gains abort_requested and the status CHECK
   accepts 'aborted_operator' — on FRESH databases (new SCHEMA_SQL) and on
   LEGACY databases (schema.migrate_purchase_attempts_abort rebuild), with
   later-migration columns and existing rows preserved.
2. Daemon abort gate: _abort_requested is flag-driven and FAIL-OPEN (any
   error -> continue purchasing). _checkout_inner has exactly 4 checkpoints
   and the last one sits BEFORE place_btn.click() — nothing after the click.
3. autobuy_v2: 'aborted_operator' breaks the card ladder before any other
   card, records no card decline, never suspends, never alerts a payment
   failure, never marks the truck missed.
4. Orchestrator: aborted_operator is TERMINAL for the roster walk (not in
   RETRYABLE_STATUSES; no advance to other orgs; truck stays 'assigned').
5. Endpoint SQL/validation: ABORT_FLAG_SQL only flips in_progress rows;
   _redirect_validate enforces truck liveness, no in-flight attempt, and
   approval-gate semantics; REDIRECT_TRUCK_RESET_SQL only touches live
   trucks; _roster_purchase_rows serves abort fields and survives a
   pre-migration roster.db.

Isolated throwaway container; temp DBs only; no network, no real browser.
"""
import inspect
import os
import re
import sqlite3
import sys
import tempfile

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        failures.append(f"{name}{(' — ' + detail) if detail else ''}")


def tmpdb():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)  # schema.create_db wants to create it
    return path


# Env BEFORE any imports that snapshot it.
ROSTER_TMP = tmpdb()
DASH_TMP = tmpdb()
os.environ["ROSTER_DB_PATH"] = ROSTER_TMP
os.environ["DASHBOARD_DB"] = DASH_TMP
sqlite3.connect(DASH_TMP).close()  # db module expects a file it can open

sys.path.insert(0, "/app/missioncontrol")
sys.path.insert(0, "/app/good360_roster")
sys.path.insert(0, "/app")

import schema  # good360_roster/schema.py

# ---------------------------------------------------------------------------
# 1. Migration
# ---------------------------------------------------------------------------
print("[1] migration — fresh schema")
fresh = tmpdb()
schema.create_db(fresh)
c = sqlite3.connect(fresh)
cols = {r[1] for r in c.execute("PRAGMA table_info(purchase_attempts)")}
check("fresh db has abort_requested", "abort_requested" in cols)
c.execute("INSERT INTO truck_events (id) VALUES (1)")
c.execute("""INSERT INTO nonprofits (id, org_name, contact_name, contact_email,
             contact_phone) VALUES (1,'O','c','e','p')""")
try:
    c.execute("INSERT INTO purchase_attempts (truck_event_id, nonprofit_id, status)"
              " VALUES (1, 1, 'aborted_operator')")
    check("fresh CHECK accepts aborted_operator", True)
except sqlite3.IntegrityError as e:
    check("fresh CHECK accepts aborted_operator", False, str(e))
try:
    c.execute("INSERT INTO purchase_attempts (truck_event_id, nonprofit_id, status)"
              " VALUES (1, 1, 'no_such_status')")
    check("fresh CHECK still rejects garbage", False)
except sqlite3.IntegrityError:
    check("fresh CHECK still rejects garbage", True)
c.commit()
# migration on an already-current db must be a silent no-op
schema.migrate_purchase_attempts_abort(fresh)
check("migration idempotent on fresh db",
      sqlite3.connect(fresh).execute(
          "SELECT COUNT(*) FROM purchase_attempts").fetchone()[0] == 1)
c.close()

print("[1b] migration — legacy db (pre-Phase-5 schema + later columns + data)")
legacy_sql = (schema.SCHEMA_SQL
              .replace(",'aborted_operator'", "")
              .replace(",\n    abort_requested     INTEGER NOT NULL DEFAULT 0", ""))
check("legacy DDL reverted for test", "aborted_operator" not in legacy_sql
      and "abort_requested" not in legacy_sql)
legacy = tmpdb()
lc = sqlite3.connect(legacy)
lc.executescript(legacy_sql)
# simulate the buyer-history migration that ran before Phase 5
lc.execute("ALTER TABLE purchase_attempts ADD COLUMN order_status TEXT")
lc.execute("ALTER TABLE purchase_attempts ADD COLUMN proof TEXT")
lc.execute("INSERT INTO truck_events (id) VALUES (7)")
lc.execute("""INSERT INTO nonprofits (id, org_name, contact_name, contact_email,
              contact_phone) VALUES (7,'Legacy Org','c','e','p')""")
lc.execute("""INSERT INTO purchase_attempts (id, truck_event_id, nonprofit_id,
              status, confirmation_number, order_status, proof)
              VALUES (42, 7, 7, 'success', 'CONF-42', 'delivered', 'proof.png')""")
lc.commit()
lc.close()
schema.migrate_purchase_attempts_abort(legacy)
lc = sqlite3.connect(legacy)
lc.row_factory = sqlite3.Row
cols = {r[1] for r in lc.execute("PRAGMA table_info(purchase_attempts)")}
check("legacy migration added abort_requested", "abort_requested" in cols)
row = lc.execute("SELECT * FROM purchase_attempts WHERE id=42").fetchone()
check("legacy row preserved through rebuild",
      row is not None and row["confirmation_number"] == "CONF-42"
      and row["order_status"] == "delivered" and row["proof"] == "proof.png"
      and row["abort_requested"] == 0,
      f"row={dict(row) if row else None}")
try:
    lc.execute("INSERT INTO purchase_attempts (truck_event_id, nonprofit_id, status)"
               " VALUES (7, 7, 'aborted_operator')")
    check("legacy CHECK widened to accept aborted_operator", True)
except sqlite3.IntegrityError as e:
    check("legacy CHECK widened to accept aborted_operator", False, str(e))
try:
    lc.execute("INSERT INTO purchase_attempts (truck_event_id, nonprofit_id, status)"
               " VALUES (7, 7, 'bogus')")
    check("legacy CHECK still rejects garbage", False)
except sqlite3.IntegrityError:
    check("legacy CHECK still rejects garbage", True)
idx = {r[0] for r in lc.execute(
    "SELECT name FROM sqlite_master WHERE type='index' "
    "AND tbl_name='purchase_attempts'")}
check("indexes recreated after rebuild",
      {"idx_pa_truck", "idx_pa_nonprofit", "idx_pa_status"} <= idx, str(idx))
# re-run must be a no-op
schema.migrate_purchase_attempts_abort(legacy)
lc.close()

# ---------------------------------------------------------------------------
# 2. Daemon abort gate
# ---------------------------------------------------------------------------
print("[2] daemon _abort_requested (fail-open) + checkpoint placement")
import good360_daemon as gd

flagdb = tmpdb()
fc = sqlite3.connect(flagdb)
fc.execute("CREATE TABLE purchase_attempts (id INTEGER PRIMARY KEY, "
           "status TEXT, abort_requested INTEGER DEFAULT 0)")
fc.execute("INSERT INTO purchase_attempts VALUES (1, 'in_progress', 1)")
fc.execute("INSERT INTO purchase_attempts VALUES (2, 'in_progress', 0)")
fc.commit()
fc.close()

gd.ROSTER_DB_PATH = flagdb
check("flag set -> aborted", gd._abort_requested(1) is True)
check("flag unset -> continue", gd._abort_requested(2) is False)
check("unknown attempt -> continue (fail-open)", gd._abort_requested(999) is False)
check("no attempt_id -> continue", gd._abort_requested(None) is False)
gd.ROSTER_DB_PATH = "/nonexistent/nowhere.db"
check("missing db -> continue (fail-open)", gd._abort_requested(1) is False)
nocol = tmpdb()
sqlite3.connect(nocol).execute(
    "CREATE TABLE purchase_attempts (id INTEGER PRIMARY KEY, status TEXT)").connection.commit()
gd.ROSTER_DB_PATH = nocol
check("unmigrated db (no column) -> continue (fail-open)",
      gd._abort_requested(1) is False)

src = inspect.getsource(gd.BrowserManager._checkout_inner)
calls = [m.start() for m in re.finditer(r"_abort_checkpoint\(", src)
         if not src[max(0, m.start() - 4):m.start()].endswith("def ")]
check("exactly 4 abort checkpoints in _checkout_inner",
      len(calls) == 4, f"found {len(calls)}")
# Anchor on the actual click CALL (the checkpoint helper's docstring also
# mentions place_btn.click, so a bare substring would hit the wrong spot).
click_at = src.index("place_btn.click(timeout=")
check("last checkpoint sits BEFORE place_btn.click()",
      calls and max(calls) < click_at)
after_click = src[click_at:]
check("no abort check or ABORTED path after the click",
      "_abort_checkpoint(" not in after_click and "'ABORTED'" not in after_click
      and "_abort_requested(" not in after_click)
check("checkpoint names match design",
      all(n in src for n in ("after_login", "after_add_to_cart",
                             "before_place_order_wait", "before_place_order_click")))
check("_abort_requested is wrapped fail-open",
      "return False" in inspect.getsource(gd._abort_requested)
      and "except Exception" in inspect.getsource(gd._abort_requested))
check("/test_checkout forwards attempt_id",
      "attempt_id=attempt_id" in inspect.getsource(gd.Handler._dispatch_post))

# ---------------------------------------------------------------------------
# 3. autobuy_v2 abort semantics
# ---------------------------------------------------------------------------
print("[3] autobuy_v2 — ladder break + no side effects on abort")
import good360_autobuy_v2 as ab

dsrc = inspect.getsource(ab._run_checkout_via_daemon)
check("attempt_id sent to daemon", '"attempt_id": attempt_id' in dsrc)
check("ABORTED maps to aborted_operator",
      re.search(r'd_status == "ABORTED".*?status="aborted_operator"', dsrc, re.S))

asrc = inspect.getsource(ab.attempt_purchase)
m = re.search(r'if result\.status == "aborted_operator":(.*?)\n            if result\.status == "daemon_unreachable"',
              asrc, re.S)
check("abort branch exists in the card-ladder failure path", m is not None)
branch = m.group(1) if m else ""
check("abort branch returns (breaks the ladder)", "return result" in branch)
check("abort branch records the attempt as aborted_operator",
      'status="aborted_operator"' in branch)
check("abort branch records NO card decline", "record_card_decline" not in branch)
check("abort branch never suspends the org", "_suspend_org_autobuy" not in branch)
check("abort branch never alerts a payment failure",
      "_alert_payment_failure" not in branch)
check("abort branch never writes truck status (stays assigned)",
      "UPDATE truck_events" not in branch and "'missed'" not in branch)
check("abort branch alerts the ADMIN channel",
      "_alert_operator_abort" in branch)
check("abort branch runs before any decline bookkeeping",
      asrc.index('== "aborted_operator"') < asrc.index("record_card_decline("))
check("abort alert is ADMIN-only",
      "telegram_router.ADMIN" in inspect.getsource(ab._alert_operator_abort)
      and "telegram_router.NGO" not in inspect.getsource(ab._alert_operator_abort))
check("attempt_id threaded into run_checkout_sequence",
      "attempt_id=attempt_id" in asrc)
check("run_checkout_sequence threads attempt_id to the daemon path",
      "attempt_id=attempt_id" in inspect.getsource(ab.run_checkout_sequence))
# guardrail / suspension logic untouched beyond the abort skip
check("card guardrail block still present", "STRICT CARD GUARDRAIL" in asrc)
check("suspension still wired for card declines", "_suspend_org_autobuy(" in asrc)

# ---------------------------------------------------------------------------
# 4. Orchestrator: aborted_operator terminal for the walk
# ---------------------------------------------------------------------------
print("[4] orchestrator walk terminates on aborted_operator")
import queue_manager
import roster_orchestrator

check("aborted_operator NOT retryable",
      "aborted_operator" not in roster_orchestrator.RETRYABLE_STATUSES)

db = str(queue_manager.DB_PATH)
os.makedirs(os.path.dirname(db), exist_ok=True)
if os.path.exists(db):
    os.remove(db)
r = sqlite3.connect(db)
r.executescript("""
CREATE TABLE nonprofits (id INTEGER PRIMARY KEY, org_name TEXT, status TEXT,
  auto_buy_global INTEGER, queue_position INTEGER, cooldown_until TEXT,
  manual_rank INTEGER, last_purchase_date TEXT, updated_at TEXT,
  max_price_override REAL);
CREATE TABLE system_config (key TEXT PRIMARY KEY, value TEXT);
INSERT INTO system_config VALUES ('cooldown_days','7');
CREATE TABLE truck_events (id INTEGER PRIMARY KEY, uuid TEXT,
  detected_at TEXT DEFAULT (datetime('now')), truck_title TEXT, truck_url TEXT,
  truck_price REAL, truck_location TEXT, truck_category TEXT,
  raw_data_json TEXT, status TEXT DEFAULT 'detected',
  assigned_to_org_id INTEGER, assigned_at TEXT, notes TEXT);
CREATE TABLE nonprofit_category_preferences (id INTEGER PRIMARY KEY,
  nonprofit_id INTEGER, category_key TEXT, category_label TEXT,
  auto_buy_enabled INTEGER, max_price_override REAL, is_excluded INTEGER);
CREATE TABLE system_events (id INTEGER PRIMARY KEY, event_type TEXT,
  severity TEXT, nonprofit_id INTEGER, message TEXT, metadata_json TEXT,
  created_at TEXT DEFAULT (datetime('now')));
INSERT INTO nonprofits (id, org_name, status, auto_buy_global, queue_position, manual_rank)
  VALUES (1,'First','active',1,1,0), (2,'Second','active',1,2,1), (3,'Third','active',1,3,2);
INSERT INTO truck_events (id, truck_title, truck_url, truck_price, truck_category, status)
  VALUES (88,'Abort Truck','http://x',100,'other','detected');
""")
r.commit()
r.close()

from types import SimpleNamespace
calls = []
def _aborted_attempt(org_id, event_id):
    calls.append(org_id)
    return SimpleNamespace(success=False, status="aborted_operator",
                           error_message="[DAEMON] aborted by operator at checkpoint after_login")
roster_orchestrator.get_autobuy = lambda: (_aborted_attempt, None, None)
result = roster_orchestrator.handle_truck_event(88)
check("walk stops after the aborted org (no auto-advance)", calls == [1], str(calls))
check("aborted result returned to caller",
      getattr(result, "status", None) == "aborted_operator")
r = sqlite3.connect(db)
tstatus = r.execute("SELECT status FROM truck_events WHERE id=88").fetchone()[0]
r.close()
check("truck stays 'assigned' (not missed) after abort", tstatus == "assigned", tstatus)

# ---------------------------------------------------------------------------
# 5. Endpoint SQL / validation predicates
# ---------------------------------------------------------------------------
print("[5] admin_routes — abort UPDATE + redirect validation")
import admin_routes

# 5a. ABORT_FLAG_SQL only flips in_progress rows
edb = tmpdb()
ec = sqlite3.connect(edb)
ec.executescript("""
CREATE TABLE purchase_attempts (id INTEGER PRIMARY KEY, truck_event_id INT,
  nonprofit_id INT, status TEXT, abort_requested INTEGER DEFAULT 0,
  started_at TEXT DEFAULT (datetime('now')), completed_at TEXT, mode TEXT,
  attempt_number INT, error_message TEXT, screenshot_path TEXT,
  confirmation_number TEXT, order_total REAL, cooldown_applied INT,
  order_status TEXT, order_status_source TEXT, order_status_updated_at TEXT);
CREATE TABLE truck_events (id INTEGER PRIMARY KEY, truck_title TEXT,
  truck_url TEXT, truck_price REAL, status TEXT,
  assigned_to_org_id INTEGER, assigned_at TEXT);
CREATE TABLE nonprofits (id INTEGER PRIMARY KEY, org_name TEXT,
  contact_email TEXT, quickbeed_customer_id TEXT);
INSERT INTO purchase_attempts (id, truck_event_id, nonprofit_id, status)
  VALUES (1, 10, 1, 'in_progress'), (2, 11, 1, 'success');
INSERT INTO truck_events VALUES
  (10, 'Live truck', 'http://t', 100, 'assigned', 1, 'x'),
  (11, 'Bought truck', 'http://t2', 100, 'purchased', 1, 'x'),
  (12, 'Fresh truck', 'http://t3', 100, 'detected', NULL, NULL);
INSERT INTO nonprofits VALUES
  (1, 'Approved Org', 'a@x', 'QBA'),
  (2, 'No-QB Org', 'b@x', NULL),
  (3, 'Parked Org', 'c@x', 'QBP');
""")
ec.commit()
cur = ec.execute(admin_routes.ABORT_FLAG_SQL, (1,))
check("abort UPDATE flips the in_progress row", cur.rowcount == 1)
cur = ec.execute(admin_routes.ABORT_FLAG_SQL, (2,))
check("abort UPDATE refuses a terminal row", cur.rowcount == 0)
check("terminal row flag untouched",
      ec.execute("SELECT abort_requested FROM purchase_attempts WHERE id=2")
        .fetchone()[0] == 0)
ec.commit()

# 5b. _redirect_validate against temp roster + dashboard DBs
dc = sqlite3.connect(tmpdb())
dc.executescript("""
CREATE TABLE customers (id TEXT PRIMARY KEY, status TEXT, in_rotation INTEGER);
INSERT INTO customers VALUES ('QBA', 'active', 1), ('QBP', 'active', 0);
""")
dc.commit()

code, err, oid = admin_routes._redirect_validate(ec, dc, 999, None)
check("missing truck -> 404", code == 404, f"{code} {err}")
code, err, oid = admin_routes._redirect_validate(ec, dc, 11, None)
check("purchased truck -> 409", code == 409 and "purchased" in (err or ""), f"{code} {err}")
code, err, oid = admin_routes._redirect_validate(ec, dc, 10, None)
check("live truck with in-flight attempt -> 409",
      code == 409 and "in_progress" in (err or ""), f"{code} {err}")
ec.execute("UPDATE purchase_attempts SET status='aborted_operator' WHERE id=1")
ec.commit()
code, err, oid = admin_routes._redirect_validate(ec, dc, 10, None)
check("next-in-queue on live truck (post-abort) -> ok",
      code == 200 and err is None and oid is None, f"{code} {err}")
code, err, oid = admin_routes._redirect_validate(ec, dc, 10, 1)
check("approved org by roster id -> ok, resolved",
      code == 200 and oid == 1, f"{code} {err} {oid}")
code, err, oid = admin_routes._redirect_validate(ec, dc, 10, "QBA")
check("approved org by QuickBeed id -> ok, resolved",
      code == 200 and oid == 1, f"{code} {err} {oid}")
code, err, oid = admin_routes._redirect_validate(ec, dc, 10, 2)
check("org without QuickBeed id -> 409", code == 409, f"{code} {err}")
code, err, oid = admin_routes._redirect_validate(ec, dc, 10, 3)
check("org out of rotation -> 409 (approval gate)",
      code == 409 and "approval gate" in (err or ""), f"{code} {err}")
code, err, oid = admin_routes._redirect_validate(ec, dc, 10, 555)
check("unknown org -> 404", code == 404, f"{code} {err}")
dc.execute("UPDATE customers SET status='suspended' WHERE id='QBA'")
dc.commit()
code, err, oid = admin_routes._redirect_validate(ec, dc, 10, 1)
check("inactive customer -> 409 (approval gate)", code == 409, f"{code} {err}")
dc.execute("UPDATE customers SET status='active' WHERE id='QBA'")
dc.commit()

# 5c. truck reset SQL: live trucks only
cur = ec.execute(admin_routes.REDIRECT_TRUCK_RESET_SQL, (10,))
check("reset flips assigned truck to detected", cur.rowcount == 1)
check("reset cleared assignment",
      ec.execute("SELECT status, assigned_to_org_id FROM truck_events WHERE id=10")
        .fetchone() == ("detected", None))
cur = ec.execute(admin_routes.REDIRECT_TRUCK_RESET_SQL, (11,))
check("reset refuses a purchased truck", cur.rowcount == 0)
ec.commit()

# 5d. purchases feed carries the new fields (and survives a legacy db)
admin_routes.ROSTER_DB_PATH = edb
rows = admin_routes._roster_purchase_rows(days=365, limit=50)
byid = {r["attempt_id"]: r for r in rows}
check("feed exposes abort_requested",
      byid.get(1, {}).get("abort_requested") == 1, str(byid.get(1)))
check("feed exposes truck_event_id + truck_event_status",
      byid.get(1, {}).get("truck_event_id") == 10
      and byid.get(1, {}).get("truck_event_status") == "detected", str(byid.get(1)))
# legacy (pre-migration) roster.db: column absent -> feed still serves rows
ec.executescript("""
CREATE TABLE old_pa AS SELECT id, truck_event_id, nonprofit_id, status,
  started_at, completed_at, mode, attempt_number, error_message,
  screenshot_path, confirmation_number, order_total, cooldown_applied,
  order_status, order_status_source, order_status_updated_at
  FROM purchase_attempts;
DROP TABLE purchase_attempts;
ALTER TABLE old_pa RENAME TO purchase_attempts;
""")
ec.commit()
rows = admin_routes._roster_purchase_rows(days=365, limit=50)
check("feed survives a pre-migration roster.db (abort_requested=0)",
      len(rows) == 2 and all(r.get("abort_requested") == 0 for r in rows),
      f"{len(rows)} rows")
ec.close()
dc.close()

# ---------------------------------------------------------------------------
if failures:
    print("\nFAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nPASS: abort flag + 4 pre-click checkpoints (fail-open), ladder breaks "
      "clean on operator abort with zero side effects, walk terminates, "
      "endpoint SQL/validation correct, migration safe on fresh + legacy DBs")
