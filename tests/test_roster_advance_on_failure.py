"""A failing customer must never jam the roster: when attempt_purchase fails
for the selected org, handle_truck_event must exclude that org and try the
NEXT eligible org for the same truck, in operator order, until one succeeds,
the truck is gone, or the roster / attempt budget is exhausted.

Covers:
1. org1 fails (payment), org2 fails (checkout/approval-gate), org3 succeeds
   -> exactly [1, 2, 3] attempted, result is success, truck 'purchased'/'assigned'.
2. all orgs fail -> every org attempted once, truck marked 'missed',
   last failure returned.
3. truck_gone on the first attempt -> NO further orgs attempted.
4. ENABLE_AUTO_BUY kill-switch failure -> terminal after one attempt
   (all orgs would fail identically).
5. category-excluded top org -> skipped to next org, not truck-missed.
6. empty-exclusion regression: find_next_available_org() with no exclusions
   still picks (the NOT IN () / NOT IN (NULL) trap).

Isolated throwaway container; temp DBs only (same convention as
test_roster_pick_correctness.py).
"""
import os
import sqlite3
import sys
from types import SimpleNamespace

failures = []

sys.path.insert(0, "/app/good360_roster")
sys.path.insert(0, "/app")
import queue_manager
import roster_orchestrator


def fresh_db(orgs):
    """(Re)create the roster db with the given org rows."""
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
CREATE TABLE truck_events (id INTEGER PRIMARY KEY,
  uuid TEXT DEFAULT (lower(hex(randomblob(16)))),
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
""")
    for row in orgs:
        r.execute("INSERT INTO nonprofits (id, org_name, status, auto_buy_global,"
                  " queue_position, manual_rank) VALUES (?,?,?,?,?,?)", row)
    r.execute("INSERT INTO truck_events (id, truck_title, truck_url, truck_price,"
              " truck_category, status) VALUES (77,'Test Truck','http://x',100,'other','detected')")
    r.commit(); r.close()


THREE_ORGS = [(1, "First", "active", 1, 1, 0),
              (2, "Second", "active", 1, 2, 1),
              (3, "Third", "active", 1, 3, 2)]


def fake_autobuy(script):
    """attempt_purchase stub: pops the next scripted CheckoutResult-alike.
    Records the org order in `calls`."""
    calls = []

    def attempt(org_id, event_id):
        calls.append(org_id)
        kind = script[min(len(calls) - 1, len(script) - 1)]
        return {
            "pay":  SimpleNamespace(success=False, status="failed_payment",
                                    error_message="card declined"),
            "chk":  SimpleNamespace(success=False, status="failed_checkout",
                                    error_message="PURCHASE BLOCKED by approval gate: not approved"),
            "ok":   SimpleNamespace(success=True, status="success", error_message=None),
            "gone": SimpleNamespace(success=False, status="truck_gone",
                                    error_message="sold out"),
            "kill": SimpleNamespace(success=False, status="failed_checkout",
                                    error_message="auto-buy disabled in this environment (ENABLE_AUTO_BUY=false)"),
        }[kind]

    return calls, attempt


def truck_status():
    r = sqlite3.connect(str(queue_manager.DB_PATH))
    s = r.execute("SELECT status FROM truck_events WHERE id=77").fetchone()[0]
    r.close()
    return s


def run_case(name, script, expect_calls, expect_success, expect_truck_status=None):
    fresh_db(THREE_ORGS)
    calls, attempt = fake_autobuy(script)
    roster_orchestrator.get_autobuy = lambda: (attempt, None, None)
    result = roster_orchestrator.handle_truck_event(77)
    if calls != expect_calls:
        failures.append(f"{name}: attempted orgs {calls}, expected {expect_calls}")
    got_success = bool(getattr(result, "success", False))
    if got_success != expect_success:
        failures.append(f"{name}: success={got_success}, expected {expect_success} (result={result})")
    if expect_truck_status and truck_status() != expect_truck_status:
        failures.append(f"{name}: truck status={truck_status()!r}, expected {expect_truck_status!r}")


# 1. fail, fail, succeed -> walks the roster in operator order
run_case("advance-to-third", ["pay", "chk", "ok"], [1, 2, 3], True, "assigned")

# 2. everyone fails -> all tried once, truck missed
run_case("all-fail", ["pay", "pay", "pay"], [1, 2, 3], False, "missed")

# 3. truck gone -> stop immediately, don't burn the roster
run_case("truck-gone-terminal", ["gone"], [1], False)

# 4. environment kill-switch -> terminal after one attempt
run_case("kill-switch-terminal", ["kill"], [1], False)

# 5. top org excludes the category -> next org gets the truck (not truck-missed)
fresh_db(THREE_ORGS)
r = sqlite3.connect(str(queue_manager.DB_PATH))
r.execute("INSERT INTO nonprofit_category_preferences (nonprofit_id, category_key,"
          " auto_buy_enabled, is_excluded) VALUES (1,'other',1,1)")
r.commit(); r.close()
calls, attempt = fake_autobuy(["ok"])
roster_orchestrator.get_autobuy = lambda: (attempt, None, None)
result = roster_orchestrator.handle_truck_event(77)
if calls != [2]:
    failures.append(f"category-excluded-skip: attempted {calls}, expected [2]")
if not getattr(result, "success", False):
    failures.append(f"category-excluded-skip: expected success, got {result}")

# 6. empty-exclusion regression: plain pick still works
fresh_db(THREE_ORGS)
pick = queue_manager.find_next_available_org()
if pick is None or pick["id"] != 1:
    failures.append(f"plain pick broken: {pick['id'] if pick else None} (expected 1)")
pick2 = queue_manager.find_next_available_org(exclude_org_ids=(1, 2))
if pick2 is None or pick2["id"] != 3:
    failures.append(f"exclusion pick broken: {pick2['id'] if pick2 else None} (expected 3)")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: failed orgs are excluded and the roster advances; terminal "
      "statuses stop the walk; plain picks unaffected")
