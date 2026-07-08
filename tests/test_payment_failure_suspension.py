"""One-failure suspension policy (operator directive 2026-07-08).

A processor card verdict (decline/CVV/expiry/etc.) during the card ladder
suspends that customer's autobuy — roster auto_buy_global=0 + dashboard
in_rotation=0 — until the operator re-enables them. Non-card failures do
not suspend. Payment data is never touched (STRICT CARD GUARDRAIL).

1. _is_card_decline classifies processor verdicts vs infrastructure noise.
2. _suspend_org_autobuy flips ONLY eligibility flags, in both DBs.
3. attempt_purchase's final failure path is wired: suspension is gated on
   saw_card_decline and not test_mode, after _alert_payment_failure.
4. _suspend_org_autobuy contains no payment-data writes.
"""
import inspect
import os
import re
import sqlite3
import sys
import tempfile
import types

sys.path.insert(0, "/app/good360_roster")
sys.path.insert(0, "/app/missioncontrol")
sys.path.insert(0, "/app")
import good360_autobuy_v2 as ab

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        failures.append(f"{name}{(' — ' + detail) if detail else ''}")


# --- 1. decline classification ----------------------------------------------
print("[1] _is_card_decline")
check("processor decline is card-caused",
      ab._is_card_decline("[DAEMON] card declined: Payment processor rejected"))
check("cvv failure is card-caused", ab._is_card_decline("CVV check failed"))
check("expiry failure is card-caused", ab._is_card_decline("Invalid expiration date"))
check("login failure is NOT card-caused", not ab._is_card_decline("Login failed"))
check("daemon dispatch failure is NOT card-caused",
      not ab._is_card_decline("[DAEMON] checkout dispatch failed: ConnectTimeout"))
check("None is NOT card-caused", not ab._is_card_decline(None))

# --- 2. suspension flips only eligibility flags ------------------------------
print("[2] _suspend_org_autobuy")
fd, roster_path = tempfile.mkstemp(suffix=".db")
os.close(fd)
rc = sqlite3.connect(roster_path)
rc.row_factory = sqlite3.Row
rc.executescript("""
CREATE TABLE nonprofits (id INTEGER PRIMARY KEY, org_name TEXT,
    quickbeed_customer_id TEXT, auto_buy_global INTEGER DEFAULT 1);
INSERT INTO nonprofits VALUES (7, 'Test Org', 'qbcust7', 1);
""")
rc.commit()

fd, dash_path = tempfile.mkstemp(suffix=".db")
os.close(fd)
dc = sqlite3.connect(dash_path)
dc.executescript("""
CREATE TABLE customers (id TEXT PRIMARY KEY, in_rotation INTEGER DEFAULT 1);
INSERT INTO customers VALUES ('qbcust7', 1);
""")
dc.commit()


class _Ctx:
    def __init__(self, path):
        self.path = path
    def __enter__(self):
        self.c = sqlite3.connect(self.path)
        self.c.row_factory = sqlite3.Row
        return self.c
    def __exit__(self, *a):
        self.c.commit()
        self.c.close()


_orig_get_db = ab.get_db_connection
ab.get_db_connection = lambda: _Ctx(roster_path)

fake_db = types.ModuleType("db")
fake_db.get_conn = lambda: _Ctx(dash_path)
_orig_db_mod = sys.modules.get("db")
sys.modules["db"] = fake_db

fake_tr = types.ModuleType("telegram_router")
SENT = []
fake_tr.ADMIN = "admin"
fake_tr.send = lambda cat, msg, **kw: SENT.append((cat, msg)) or True
_orig_tr = sys.modules.get("telegram_router")
sys.modules["telegram_router"] = fake_tr

try:
    ab._suspend_org_autobuy(7, "card declined: test")
    ab._suspend_org_autobuy(7, "card declined: test")  # idempotent
finally:
    ab.get_db_connection = _orig_get_db
    if _orig_db_mod is not None:
        sys.modules["db"] = _orig_db_mod
    else:
        sys.modules.pop("db", None)
    if _orig_tr is not None:
        sys.modules["telegram_router"] = _orig_tr
    else:
        sys.modules.pop("telegram_router", None)

rcheck = sqlite3.connect(roster_path)
check("roster auto_buy_global -> 0",
      rcheck.execute("SELECT auto_buy_global FROM nonprofits WHERE id=7").fetchone()[0] == 0)
dcheck = sqlite3.connect(dash_path)
check("dashboard in_rotation -> 0",
      dcheck.execute("SELECT in_rotation FROM customers WHERE id='qbcust7'").fetchone()[0] == 0)
check("admin alert sent", len(SENT) == 2 and SENT[0][0] == "admin")
check("alert names the org and the re-enable path",
      "Test Org" in SENT[0][1] and "Autobuy toggle" in SENT[0][1])
os.unlink(roster_path)
os.unlink(dash_path)

# --- 3. wiring in attempt_purchase -------------------------------------------
print("[3] attempt_purchase wiring")
src = inspect.getsource(ab.attempt_purchase)
check("suspension called in final failure path", "_suspend_org_autobuy(" in src)
check("gated on saw_card_decline",
      re.search(r"if saw_card_decline:\s*(#[^\n]*\n\s*)*.*_suspend_org_autobuy", src, re.S)
      is not None or "if saw_card_decline" in src)
check("inside not-test_mode block",
      re.search(r"if not test_mode:.*_suspend_org_autobuy", src, re.S) is not None)
check("daemon_unreachable aborts BEFORE suspension can trigger",
      src.index("daemon_unreachable") < src.index("_suspend_org_autobuy("))
check("card guardrail block still present", "STRICT CARD GUARDRAIL" in src)
check("no master-card fallback call", "handle_master_card_fallback(" not in src)

# --- 4. suspension never touches payment data --------------------------------
print("[4] payment-data safety")
ssrc = inspect.getsource(ab._suspend_org_autobuy)
check("no payment_methods writes", "payment_methods" not in ssrc)
check("no card/payment table writes",
      not re.search(r"UPDATE\s+\S*(card|payment)", ssrc, re.I)
      and "card_number" not in ssrc and "card_cvv" not in ssrc)
check("only eligibility flags updated",
      "auto_buy_global = 0" in ssrc and "in_rotation = 0" in ssrc)

if failures:
    print("\nFAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nPASS: one-card-failure suspension — classify, flip flags only, "
      "alert admin, guardrail intact")
