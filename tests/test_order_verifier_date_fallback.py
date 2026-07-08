"""Order-verifier date-fallback matching (backfill for $0.00 rows, 2026-07-08).

Daemon-path successes store an evidence string ("Order placed (...)")
as confirmation_number, so sync_rows' exact order-id match never fires
and order_total stays NULL forever. The date fallback pairs such rows
with leftover Order History rows when the pairing is unambiguous.

1. Exact order-id matching still works and wins first.
2. Evidence-string row gets date-matched: total + status + real order id
   written, checkout evidence preserved in proof.
3. DRYRUN rows are never touched.
4. Ambiguous pairings (two attempts same org+date) match nothing.
5. order_status_source='manual' rows are never overwritten.
6. ±1-day skew pass catches midnight/timezone drift.
"""
import json
import os
import sqlite3
import sys
import tempfile

sys.path.insert(0, "/app/missioncontrol")
import order_verifier as ov

failures = []


def make_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE purchase_attempts (
        id INTEGER PRIMARY KEY, nonprofit_id INTEGER, status TEXT,
        confirmation_number TEXT, order_total REAL, order_status TEXT,
        order_status_source TEXT, order_status_updated_at TEXT,
        proof TEXT, completed_at TEXT)""")
    return conn, path


def seed(conn, rows):
    conn.executemany(
        """INSERT INTO purchase_attempts
           (id, nonprofit_id, status, confirmation_number, order_total,
            order_status, order_status_source, proof, completed_at)
           VALUES (?,?,?,?,?,?,?,?,?)""", rows)
    conn.commit()


def fetch(conn, rid):
    conn.row_factory = sqlite3.Row
    return conn.execute("SELECT * FROM purchase_attempts WHERE id=?", (rid,)).fetchone()


# --- mirror of the live 2026-07 situation ----------------------------------
conn, path = make_db()
seed(conn, [
    # exact-match row (the known-good 06-12 purchase)
    (36, 6, "success", "100272562", 5837.12, "approved", "auto", "{}",
     "2026-06-12 04:19:00"),
    # evidence-string rows — the 7-row bug class
    (57, 2, "success", "Order placed (url=.../checkout/success, thank-you-message)",
     None, "approved", None, "{}", "2026-07-06 14:03:00"),
    (53, 2, "success", "Order placed (thank-you-message)",
     None, "approved", None, "{}", "2026-07-02 09:00:00"),
    # DRYRUN must never be touched
    (25, 2, "success", "DRYRUN-2-28", 0.0, None, None, "{}", "2026-05-18 12:00:00"),
])

site_rows_org2 = [
    {"order_id": "100290001", "status": "Approved", "admin_fee": 6400.00, "date": "07/06/2026"},
    {"order_id": "100285555", "status": "Approved", "admin_fee": 5900.00, "date": "07/02/2026"},
]
updated = ov.sync_rows(2, site_rows_org2, roster_db=path)
if updated != 2:
    failures.append(f"expected 2 updates for org 2, got {updated}")

r57 = fetch(conn, 57)
if r57["order_total"] != 6400.00 or r57["confirmation_number"] != "100290001":
    failures.append(f"row 57 not date-matched: total={r57['order_total']} "
                    f"conf={r57['confirmation_number']}")
elif "Order placed" not in json.loads(r57["proof"]).get("checkout_evidence", ""):
    failures.append("row 57 checkout evidence not preserved in proof")

r53 = fetch(conn, 53)
if r53["order_total"] != 5900.00 or r53["confirmation_number"] != "100285555":
    failures.append(f"row 53 not date-matched: total={r53['order_total']}")

r25 = fetch(conn, 25)
if r25["confirmation_number"] != "DRYRUN-2-28" or r25["order_total"] != 0.0:
    failures.append("DRYRUN row was modified")

# exact match still first-class for another org
updated = ov.sync_rows(6, [{"order_id": "100272562", "status": "Complete",
                            "admin_fee": 5837.12, "date": "06/12/2026"}],
                       roster_db=path)
r36 = fetch(conn, 36)
if updated != 1 or r36["order_status"] != "complete":
    failures.append(f"exact match regression: updated={updated} "
                    f"status={r36['order_status']}")
os.unlink(path)

# --- ambiguity: two attempts, same org, same date → no fallback -------------
conn, path = make_db()
seed(conn, [
    (1, 9, "success", "Order placed (a)", None, None, None, "{}", "2026-07-06 10:00:00"),
    (2, 9, "success", "Order placed (b)", None, None, None, "{}", "2026-07-06 11:00:00"),
])
updated = ov.sync_rows(9, [{"order_id": "111", "status": "Approved",
                            "admin_fee": 100.0, "date": "07/06/2026"}],
                       roster_db=path)
if updated != 0:
    failures.append(f"ambiguous same-date pairing matched anyway ({updated})")
os.unlink(path)

# --- manual rows protected; ±1-day skew works --------------------------------
conn, path = make_db()
seed(conn, [
    (1, 9, "success", "Order placed (m)", None, "cancelled", "manual", "{}",
     "2026-07-06 10:00:00"),
    (2, 9, "success", "Order placed (skew)", None, None, None, "{}",
     "2026-07-07 00:10:00"),
])
updated = ov.sync_rows(9, [{"order_id": "222", "status": "Approved",
                            "admin_fee": 250.0, "date": "07/06/2026"}],
                       roster_db=path)
r1, r2 = fetch(conn, 1), fetch(conn, 2)
if r1["order_status"] != "cancelled" or r1["order_total"] is not None:
    failures.append("manual row was overwritten by fallback")
if updated != 1 or r2["order_total"] != 250.0:
    failures.append(f"±1-day skew match failed: updated={updated} "
                    f"total={r2['order_total']}")
os.unlink(path)

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: date fallback backfills evidence-string rows, preserves proof, "
      "skips DRYRUN/manual/ambiguous")
