"""Self-heal incident responder — bounded playbook tests.

HTTP, docker and telegram are all faked; no real network, no real docker.

Covers:
1. External liveness: healthy -> plain ping URL; unhealthy (stale heartbeat
   or daemon down) -> <url>/fail; HEALTHCHECKS_PING_URL unset -> no HTTP call.
2. Stale bookkeeping: in_progress rows >2h old closed to failed_checkout
   with the self-heal message; younger rows and other columns untouched.
3. Daemon wedge: 3 consecutive /health failures -> exactly one restart;
   further failures inside the 30-minute window -> NO second restart.
4. SELF_HEAL_DRY_RUN=true -> no restart call, no DB write, alerts still sent.
5. Source inspection: hard-boundary docstring present; SQL writes touch
   purchase_attempts only; no payment/card table references; no
   queue/rotation/cooldown code outside the boundary docstring.
"""
import json
import os
import re
import sqlite3
import sys
import types
from datetime import datetime, timedelta, timezone

failures = []

WORKDIR = "/app/workdir"
ROSTER_DB = f"{WORKDIR}/test_roster.db"
HEARTBEAT = f"{WORKDIR}/good360_heartbeat.json"
PING_URL = "https://hc-ping.example/test-uuid"

os.environ["WORKDIR"] = WORKDIR
os.environ["ROSTER_DB_PATH"] = ROSTER_DB
os.environ.pop("HEALTHCHECKS_PING_URL", None)
os.environ.pop("SELF_HEAL_DRY_RUN", None)

sys.path.insert(0, "/app")

# --- fakes (installed before import so no real transport is ever touched) --
tg_sent = []


def _fake_send(category, message, **kw):
    tg_sent.append((category, message, kw))
    return True


sys.modules["telegram_router"] = types.SimpleNamespace(
    ADMIN="admin", send=_fake_send)

import self_heal  # noqa: E402


class _Resp:
    def __init__(self, code):
        self.status_code = code


class FakeRequests:
    def __init__(self):
        self.gets = []
        self.daemon_code = 200

    def get(self, url, timeout=None):
        self.gets.append(url)
        if url == self_heal.DAEMON_HEALTH_URL:
            return _Resp(self.daemon_code)
        return _Resp(200)


freq = FakeRequests()
self_heal.requests = freq

restarts = []


def _fake_restart(name):
    restarts.append(name)
    return True, "ok"


self_heal._docker_restart = _fake_restart


def write_heartbeat(minutes_ago):
    ts = (datetime.now(timezone.utc)
          - timedelta(minutes=minutes_ago)).isoformat()
    with open(HEARTBEAT, "w") as f:
        json.dump({"last_scan": ts}, f)


def sqlite_ts(hours_ago):
    return (datetime.now(timezone.utc)
            - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")


def make_roster(rows):
    if os.path.exists(ROSTER_DB):
        os.remove(ROSTER_DB)
    conn = sqlite3.connect(ROSTER_DB)
    conn.execute("""CREATE TABLE purchase_attempts (
        id INTEGER PRIMARY KEY, status TEXT, started_at TEXT,
        error_message TEXT, completed_at TEXT, order_total REAL,
        confirmation_number TEXT)""")
    conn.executemany(
        "INSERT INTO purchase_attempts "
        "(id, status, started_at, error_message, completed_at, order_total, "
        "confirmation_number) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()


def fetch(att_id):
    conn = sqlite3.connect(ROSTER_DB)
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT * FROM purchase_attempts WHERE id = ?", (att_id,)).fetchone()
    conn.close()
    return row


# ---------------------------------------------------------------------------
# 1. External liveness
# ---------------------------------------------------------------------------
os.environ["HEALTHCHECKS_PING_URL"] = PING_URL

write_heartbeat(1)
freq.daemon_code = 200
ok, _ = self_heal.probe_daemon()
self_heal.check_external_liveness(ok)
if freq.gets[-1] != PING_URL:
    failures.append(f"healthy state pinged {freq.gets[-1]!r}, expected plain URL")

write_heartbeat(30)  # stale heartbeat -> unhealthy
ok, _ = self_heal.probe_daemon()
self_heal.check_external_liveness(ok)
if freq.gets[-1] != PING_URL + "/fail":
    failures.append(f"stale heartbeat pinged {freq.gets[-1]!r}, expected /fail")

write_heartbeat(1)
freq.daemon_code = 500  # daemon down -> unhealthy
ok, _ = self_heal.probe_daemon()
if ok:
    failures.append("probe_daemon returned healthy for HTTP 500")
self_heal.check_external_liveness(ok)
if freq.gets[-1] != PING_URL + "/fail":
    failures.append(f"daemon-down pinged {freq.gets[-1]!r}, expected /fail")
freq.daemon_code = 200

del os.environ["HEALTHCHECKS_PING_URL"]
n_gets = len(freq.gets)
self_heal.check_external_liveness(True)
if len(freq.gets) != n_gets:
    failures.append("liveness made an HTTP call with no HEALTHCHECKS_PING_URL")

# ---------------------------------------------------------------------------
# 2. Stale in_progress purchase attempts
# ---------------------------------------------------------------------------
make_roster([
    (1, "in_progress", sqlite_ts(3), None, None, 123.45, None),   # stale
    (2, "in_progress", sqlite_ts(0.5), None, None, None, None),   # young
    (3, "success", sqlite_ts(50), None, sqlite_ts(49), 99.0, "CONF-1"),
])
n_alerts = len(tg_sent)
self_heal.check_stale_attempts()

r1, r2, r3 = fetch(1), fetch(2), fetch(3)
if r1["status"] != "failed_checkout":
    failures.append(f"stale row not closed: status={r1['status']!r}")
if ("[self-heal] stale in_progress attempt closed" not in (r1["error_message"] or "")
        or r1["started_at"] not in (r1["error_message"] or "")
        or ">2h" not in (r1["error_message"] or "")):
    failures.append(f"stale row error_message wrong: {r1['error_message']!r}")
if r1["order_total"] != 123.45 or r1["completed_at"] is not None \
        or r1["confirmation_number"] is not None:
    failures.append("self-heal modified columns other than status/error_message")
if r2["status"] != "in_progress" or r2["error_message"] is not None:
    failures.append("row younger than 2h was touched")
if r3["status"] != "success" or r3["error_message"] is not None \
        or r3["confirmation_number"] != "CONF-1":
    failures.append("non-in_progress row was touched")
if len(tg_sent) != n_alerts + 1:
    failures.append(f"stale close sent {len(tg_sent) - n_alerts} alerts, expected 1")

# ---------------------------------------------------------------------------
# 3. Daemon wedge: restart after 3 consecutive fails, rate-limited to one
# ---------------------------------------------------------------------------
freq.daemon_code = 503
state = {}
hist = []
for _ in range(3):
    ok, detail = self_heal.probe_daemon()
    self_heal.check_daemon_wedge(state, ok, detail, hist)
if restarts != [self_heal.DAEMON_CONTAINER]:
    failures.append(f"after 3 bad cycles expected one daemon restart, got {restarts}")
restart_alerts = [m for _, m, _ in tg_sent if "restarting" in m]
if not restart_alerts or "HTTP 503" not in restart_alerts[-1]:
    failures.append("daemon restart alert missing the probe errors")
if "last_daemon_restart" not in json.load(open(self_heal.STATE_FILE)):
    failures.append("last_daemon_restart not persisted to the state file")

for _ in range(4):  # cycles 4-7 still unhealthy, inside the 30-min window
    ok, detail = self_heal.probe_daemon()
    self_heal.check_daemon_wedge(state, ok, detail, hist)
if len(restarts) != 1:
    failures.append(f"second restart fired inside the rate-limit window: {restarts}")
freq.daemon_code = 200

# ---------------------------------------------------------------------------
# 4. SELF_HEAL_DRY_RUN=true: no restart, no DB write, alerts still flow
# ---------------------------------------------------------------------------
os.environ["SELF_HEAL_DRY_RUN"] = "true"
restarts.clear()
state = {}
hist = []
freq.daemon_code = 500
n_alerts = len(tg_sent)
for _ in range(3):
    ok, detail = self_heal.probe_daemon()
    self_heal.check_daemon_wedge(state, ok, detail, hist)
if restarts:
    failures.append(f"dry run performed a restart: {restarts}")
dry_alerts = [m for _, m, _ in tg_sent[n_alerts:] if "DRY RUN" in m]
if not dry_alerts:
    failures.append("dry run daemon wedge did not exercise the alert path")
freq.daemon_code = 200

make_roster([(1, "in_progress", sqlite_ts(3), None, None, None, None)])
n_alerts = len(tg_sent)
self_heal.check_stale_attempts()
r1 = fetch(1)
if r1["status"] != "in_progress" or r1["error_message"] is not None:
    failures.append("dry run wrote to the roster DB")
if len(tg_sent) != n_alerts + 1:
    failures.append("dry run stale-attempt alert path not exercised")
del os.environ["SELF_HEAL_DRY_RUN"]

# ---------------------------------------------------------------------------
# 5. Source inspection: boundaries are encoded and honored
# ---------------------------------------------------------------------------
src = open("/app/self_heal.py").read()
doc = self_heal.__doc__ or ""
if "HARD BOUNDARIES" not in doc:
    failures.append("hard-boundary docstring missing from self_heal module")
body = src.replace(doc, "")
for word in ("queue", "rotation", "cooldown", "suspend"):
    if word in body:
        failures.append(f"{word!r} referenced outside the boundary docstring")
for token in ("system_payment_methods", "nonprofit_payment_methods",
              "master_card_transactions", "payment_method_id",
              "card_number", "card_cvv", "telegram_channels"):
    if token in src:
        failures.append(f"forbidden payment/card reference in source: {token}")
sql_writes = set(re.findall(
    r"(?i)\b(?:update|insert\s+into|delete\s+from)\s+([A-Za-z_]+)", src))
if sql_writes - {"purchase_attempts"}:
    failures.append(f"SQL writes outside purchase_attempts: {sql_writes}")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: self-heal playbook bounded and honored — liveness pings, stale "
      "bookkeeping closes, rate-limited restarts, dry-run inert, no "
      "payment/card reach")
