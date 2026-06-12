"""Failing-first test: customer buyer-history query must return rows when
where_sql + params are combined with the default days window (placeholder
ordering bug: customer id was being bound into datetime('now', ?))."""
import os, sqlite3, sys

os.environ["ROSTER_DB_PATH"] = "/tmp/test_roster.db"
os.environ["DASHBOARD_DB"] = "/tmp/test_dash.db"

r = sqlite3.connect("/tmp/test_roster.db")
r.executescript("""
CREATE TABLE purchase_attempts (id INTEGER PRIMARY KEY, truck_event_id INT,
  nonprofit_id INT, started_at TEXT, completed_at TEXT, status TEXT, mode TEXT,
  attempt_number INT, error_message TEXT, screenshot_path TEXT,
  confirmation_number TEXT, order_total REAL, cooldown_applied INT,
  order_status TEXT, order_status_source TEXT, order_status_updated_at TEXT,
  proof TEXT);
CREATE TABLE truck_events (id INTEGER PRIMARY KEY, truck_title TEXT, truck_url TEXT, truck_price REAL);
CREATE TABLE nonprofits (id INTEGER PRIMARY KEY, org_name TEXT, contact_email TEXT, quickbeed_customer_id TEXT);
INSERT INTO nonprofits VALUES (6, 'Test Org', 'x@y.z', 'QB123');
INSERT INTO truck_events VALUES (57, 'Test Truck', 'http://x', 0);
INSERT INTO purchase_attempts (id, truck_event_id, nonprofit_id, started_at,
  completed_at, status, mode, attempt_number, confirmation_number, cooldown_applied)
  VALUES (36, 57, 6, datetime('now','-1 hour'),
  datetime('now'), 'success', 'auto_buy', 1, '100272562', 1);
""")
r.commit(); r.close()

sys.path.insert(0, "/app/missioncontrol")
import admin_routes

rows = admin_routes._roster_purchase_rows(
    where_sql="np.quickbeed_customer_id = ?", params=("QB123",), days=90, limit=200)
if len(rows) != 1 or rows[0]["confirmation_number"] != "100272562":
    print(f"FAIL: expected the success row, got {len(rows)} rows")
    sys.exit(1)
# the kwarg path must keep working too
rows2 = admin_routes._roster_purchase_rows(customer_id="QB123", days=90, limit=200)
if len(rows2) != 1:
    print(f"FAIL: customer_id kwarg path broken, got {len(rows2)} rows")
    sys.exit(1)
print("PASS: buyer history returns rows with where_sql + days combined")
