"""Phase 3: queue-remove + Cool-off panel fix.

1. Functional: the Cool-off panel query (ROSTER_COOLDOWN_SQL, used by
   GET /api/admin/roster/queue) only shows customers the round-robin could
   actually pick again when the timer lapses — cooling rows the operator
   pulled from rotation (in_rotation = 0) or deactivated must be hidden
   immediately, not sit visibly until the timer runs out.
2. Source inspection: admin.js offers "Remove from queue" (rotation PATCH,
   in_rotation off) on both the cool-off chips and the drag-queue list
   items, and the old "✕ clear" button is relabeled to "Re-queue now" with
   a title that says the customer becomes eligible again.
"""
import re
import sqlite3
import sys

ROUTES = "/app/missioncontrol/admin_routes.py"
ADMIN_JS = "/app/missioncontrol/static/admin.js"

routes_src = open(ROUTES, encoding="utf-8").read()
js_src = open(ADMIN_JS, encoding="utf-8").read()

failures = []

# ---- 1. Functional: run the exact Cool-off SQL against a temp db ----
m = re.search(r'ROSTER_COOLDOWN_SQL = """(.*?)"""', routes_src, re.S)
if not m:
    failures.append("ROSTER_COOLDOWN_SQL constant not found in admin_routes.py")
else:
    sql = m.group(1)
    if "c.execute(ROSTER_COOLDOWN_SQL)" not in routes_src:
        failures.append("roster_queue does not execute ROSTER_COOLDOWN_SQL — "
                        "the panel query and the tested SQL have diverged")
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        """CREATE TABLE customers (
               id TEXT PRIMARY KEY, organization_name TEXT, full_name TEXT,
               cooldown_until TEXT, last_used_at TEXT, last_purchase_at TEXT,
               status TEXT, in_rotation INTEGER)"""
    )
    #                 id     org             cooldown offset  status     in_rotation
    seed = [
        ("c1", "Shown Org",     "+2 days", "active", 1),  # cooling, in rotation → shown
        ("c2", "Derotated Org", "+2 days", "active", 0),  # removed from queue  → hidden
        ("c3", "Paused Org",    "+2 days", "paused", 1),  # not active          → hidden
        ("c4", "Expired Org",   "-1 days", "active", 1),  # cooldown lapsed     → hidden
    ]
    for cid, org, offset, status, rot in seed:
        conn.execute(
            "INSERT INTO customers VALUES (?,?,?,datetime('now', ?),NULL,NULL,?,?)",
            (cid, org, org, offset, status, rot))
    shown = [r["id"] for r in conn.execute(sql).fetchall()]
    if shown != ["c1"]:
        failures.append(
            f"Cool-off SQL returned {shown}, expected ['c1'] — must show only "
            "cooling customers with status='active' AND in_rotation=1")
    conn.close()

# ---- 2. Source inspection: admin.js wiring ----
if "Remove from queue" not in js_src:
    failures.append('admin.js has no "Remove from queue" action')

fn = re.search(r"async function removeCustomerFromQueue\b(.*?)\n\}", js_src, re.S)
if not fn:
    failures.append("removeCustomerFromQueue() not found in admin.js")
else:
    body = fn.group(1)
    if "/rotation" not in body or "'PATCH'" not in body:
        failures.append("removeCustomerFromQueue does not PATCH the /rotation endpoint")
    if not re.search(r"in_rotation:\s*(0|false)", body):
        failures.append("removeCustomerFromQueue does not send in_rotation off")
    if "confirm(" not in body:
        failures.append("removeCustomerFromQueue has no confirm dialog")

if "cooldown-remove" not in js_src:
    failures.append("cool-off chips have no remove button (.cooldown-remove)")
if "queue-remove" not in js_src:
    failures.append("queue list items have no remove button (.queue-remove)")
if js_src.count("removeCustomerFromQueue") < 3:
    failures.append("removeCustomerFromQueue not wired to both the cool-off "
                    "chips and the queue list (expected definition + 2 call sites)")

if "✕ clear" in js_src:
    failures.append('bare "✕ clear" button label still present — must be relabeled')
if "Re-queue now" not in js_src:
    failures.append('relabeled "Re-queue now" button not found')
if "eligible for autobuy again" not in js_src:
    failures.append("Re-queue button title does not say the customer becomes "
                    "eligible again")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: cool-off panel hides de-rotated customers; Remove-from-queue and "
      "Re-queue-now wired in the UI")
