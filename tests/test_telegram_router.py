"""Telegram router: the cross-customer fan-out leak is dead.

Covers (HTTP fully mocked — never hits the network):
  1. seed migration creates admin + 2 ngo channels from legacy settings,
     and is idempotent (init_db twice -> no duplicates)
  2. ADMIN routes only to admin channels
  3. NGO routes only to the matching org's channel (by org_key and org_id)
  4. NGO with no matching channel falls back to admin channels ONLY —
     never to another org's channel
  5. GENERAL falls back to admin when no general channel, then routes to a
     general channel once one exists
  6. notifications_enabled()==False -> no send, returns False
  7. one channel's HTTP failure doesn't block the next; last_error/last_sent_at
     land on the right rows
  8. source inspection: monitor has no all-groups fan-out; report has no
     hardcoded chat IDs
"""
import os
import re
import sys
import tempfile

# --- environment BEFORE any project import -------------------------------
os.environ["ENABLE_NOTIFICATIONS"] = "true"
os.environ.pop("DASHBOARD_MASTER_KEY", None)
os.environ["DASHBOARD_DB"] = os.path.join(
    tempfile.mkdtemp(prefix="tg_router_test_"), "dashboard.db")
os.environ["TELEGRAM_BOT_TOKEN"] = "TEST:TOKEN"
os.environ["TELEGRAM_OPERATOR_CHAT_ID"] = "-100000001"
os.environ["TELEGRAM_GROUP_HOPE4HUMANITY"] = "-100000002"
os.environ["TELEGRAM_GROUP_REVIVING_HOMES"] = "-100000003"

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/missioncontrol")

import db  # noqa: E402  (missioncontrol/db.py)
import telegram_router as tr  # noqa: E402

failures = []


def check(name, cond, detail=""):
    if cond:
        print(f"  ok: {name}")
    else:
        failures.append(f"{name}{' — ' + detail if detail else ''}")
        print(f"  FAIL: {name} {detail}")


# --- HTTP mock ------------------------------------------------------------
SENT = []          # (chat_id, text, payload)
FAIL_CHATS = set()  # chat_ids whose send should blow up


class _FakeResp:
    status_code = 200
    text = '{"ok": true}'

    def json(self):
        return {"ok": True}


def fake_post(url, json=None, timeout=None):
    chat = json["chat_id"]
    if chat in FAIL_CHATS:
        raise ConnectionError(f"simulated network failure for {chat}")
    SENT.append((chat, json.get("text", ""), json))
    return _FakeResp()


tr.requests.post = fake_post  # monkeypatch — no real HTTP ever


def sent_chats():
    return [c for c, _, _ in SENT]


# --- 1. seed migration + idempotence ---------------------------------------
print("[1] seed migration")
db.init_db()
db.init_db()  # second run must not duplicate
with db.get_conn() as c:
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM telegram_channels ORDER BY id").fetchall()]
check("seed created exactly 3 channels", len(rows) == 3, f"got {len(rows)}")
by_chat = {r["chat_id"]: r for r in rows}
check("operator seeded as admin",
      by_chat.get("-100000001", {}).get("category") == "admin")
check("h4h seeded as ngo/org_key",
      by_chat.get("-100000002", {}).get("category") == "ngo"
      and by_chat.get("-100000002", {}).get("org_key") == "hope4humanity"
      and by_chat.get("-100000002", {}).get("org_id") == 1)
check("reviving homes seeded as ngo/org_key",
      by_chat.get("-100000003", {}).get("category") == "ngo"
      and by_chat.get("-100000003", {}).get("org_key") == "reviving_homes"
      and by_chat.get("-100000003", {}).get("org_id") == 6)

# --- 2. ADMIN routing -------------------------------------------------------
print("[2] admin routing")
SENT.clear()
ok = tr.send(tr.ADMIN, "admin msg", source="test")
check("admin send returns True", ok is True)
check("admin goes only to admin channel", sent_chats() == ["-100000001"],
      f"got {sent_chats()}")

# --- 3. NGO routing to the right org ----------------------------------------
print("[3] ngo routing")
SENT.clear()
tr.send(tr.NGO, "h4h msg", org_key="hope4humanity", source="test")
check("ngo org_key match -> only that org", sent_chats() == ["-100000002"],
      f"got {sent_chats()}")
SENT.clear()
tr.send(tr.NGO, "revh msg", org_id=6, source="test")
check("ngo org_id match -> only that org", sent_chats() == ["-100000003"],
      f"got {sent_chats()}")
check("no cross-org delivery ever", "-100000002" not in sent_chats())

# --- 4. NGO with no matching channel -> admin only ---------------------------
print("[4] ngo fallback")
SENT.clear()
ok = tr.send(tr.NGO, "unknown org msg", org_key="brand_new_org", source="test")
check("ngo no-match returns True (delivered to admin)", ok is True)
check("ngo no-match -> admin channel only", sent_chats() == ["-100000001"],
      f"got {sent_chats()}")
check("ngo no-match never hits other orgs",
      "-100000002" not in sent_chats() and "-100000003" not in sent_chats())
SENT.clear()
tr.send(tr.NGO, "orgless msg", source="test")  # no org at all
check("ngo without org -> admin only", sent_chats() == ["-100000001"],
      f"got {sent_chats()}")

# --- 5. GENERAL routing ------------------------------------------------------
print("[5] general routing")
SENT.clear()
tr.send(tr.GENERAL, "truck available", source="test")
check("general falls back to admin when unconfigured",
      sent_chats() == ["-100000001"], f"got {sent_chats()}")
with db.get_conn() as c:
    c.execute("""INSERT INTO telegram_channels (chat_id, title, category)
                 VALUES ('-100000009', 'Alerts', 'general')""")
SENT.clear()
tr.send(tr.GENERAL, "truck available", source="test")
check("general routes to general channel once configured",
      sent_chats() == ["-100000009"], f"got {sent_chats()}")

# --- 6. notifications gate ---------------------------------------------------
print("[6] notifications gate")
os.environ["ENABLE_NOTIFICATIONS"] = "false"
SENT.clear()
ok = tr.send(tr.ADMIN, "should not go out", source="test")
check("gated send returns False", ok is False)
check("gated send makes zero HTTP calls", SENT == [], f"got {sent_chats()}")
os.environ["ENABLE_NOTIFICATIONS"] = "true"

# --- 7. per-channel failure isolation ---------------------------------------
print("[7] failure isolation")
with db.get_conn() as c:
    c.execute("""INSERT INTO telegram_channels (chat_id, title, category)
                 VALUES ('-100000010', 'Second admin', 'admin')""")
FAIL_CHATS.add("-100000001")
SENT.clear()
ok = tr.send(tr.ADMIN, "partial failure", source="test")
FAIL_CHATS.clear()
check("send still True when one channel fails", ok is True)
check("healthy channel still reached", sent_chats() == ["-100000010"],
      f"got {sent_chats()}")
with db.get_conn() as c:
    bad = c.execute("SELECT last_error, last_sent_at FROM telegram_channels "
                    "WHERE chat_id='-100000001'").fetchone()
    good = c.execute("SELECT last_error, last_sent_at FROM telegram_channels "
                     "WHERE chat_id='-100000010'").fetchone()
check("failing channel records last_error",
      bad["last_error"] is not None and "simulated" in bad["last_error"],
      f"got {bad['last_error']!r}")
check("healthy channel has last_sent_at and no error",
      good["last_sent_at"] is not None and good["last_error"] is None)

# --- 8. source inspection: fan-out + hardcodes are gone ----------------------
print("[8] source inspection")
monitor_src = open("/app/good360_monitor.py", encoding="utf-8").read()
check("monitor: ALL_TELEGRAM_GROUPS fan-out is gone",
      "ALL_TELEGRAM_GROUPS" not in monitor_src)
check("monitor: no all-orgs channel label left",
      "'all-orgs'" not in monitor_src and '"all-orgs"' not in monitor_src)
report_src = open("/app/good360_report.py", encoding="utf-8").read()
check("report: no hardcoded chat IDs",
      not re.search(r"-\d{9,}", report_src))
check("report: TELEGRAM_CHAT_IDS list removed",
      "TELEGRAM_CHAT_IDS" not in report_src)

# ----------------------------------------------------------------------------
if failures:
    print("\nFAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("\nPASS: telegram router — routing, isolation, gate, seed all verified")
