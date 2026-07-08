"""The daemon port must survive a wedged browser call (June 16 2026 incident:
one hung Playwright call + single-threaded HTTPServer + 5-deep backlog =
2 weeks of ConnectTimeout while docker said 'healthy').

Covers:
1. BrowserWorker basics: jobs run on the dedicated browser thread, results
   and exceptions propagate, a slow job raises TimeoutError at the deadline.
2. THE incident shape: while a checkout is stuck on the browser thread,
   GET /health still answers instantly and a second checkout gets a fast
   503 (worker busy) instead of hanging the socket.
3. v2 dispatch classification: ConnectTimeout/ConnectionError from the
   daemon → status 'daemon_unreachable' (infra), NOT a card-verdict status.

Isolated throwaway container; no real browser (manager.start is stubbed).
"""
import json
import os
import sys
import threading
import time
import urllib.request

failures = []

# Endpoint deadlines must be short for the test — set BEFORE import.
os.environ["DAEMON_CHECKOUT_WORKER_TIMEOUT"] = "3"

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/good360_roster")
import good360_daemon as d

# ---------------------------------------------------------------------------
# 1. BrowserWorker basics
# ---------------------------------------------------------------------------
d.manager.start = lambda: None          # no real browser
d.worker.start()
if not d.worker.ready.wait(10):
    failures.append("worker never became ready")

thread_seen = {}
def _whoami():
    thread_seen["name"] = threading.current_thread().name
    return 42

if d.worker.call(_whoami, timeout=5) != 42:
    failures.append("worker.call did not return the job result")
if thread_seen.get("name") != "browser-worker":
    failures.append(f"job ran on {thread_seen.get('name')!r}, expected browser-worker")

def _boom():
    raise ValueError("kaput")
try:
    d.worker.call(_boom, timeout=5)
    failures.append("worker.call swallowed the job exception")
except ValueError:
    pass

t0 = time.time()
try:
    d.worker.call(time.sleep, 30, timeout=1)
    failures.append("slow job did not raise TimeoutError")
except TimeoutError:
    if time.time() - t0 > 5:
        failures.append("TimeoutError came far too late")
# let the sleeping job finish in the worker before the next phase
time.sleep(0.2)

# ---------------------------------------------------------------------------
# 2. Incident shape: stuck checkout must not seal the port
# ---------------------------------------------------------------------------
WEDGE = threading.Event()
def fake_checkout(org_key, org_config, truck_name, truck_url, force_login=False):
    WEDGE.wait(60)  # simulate a Playwright call that never returns
    return ("MISSED", "late", 0.0)

d.manager.checkout = fake_checkout
d.manager.contexts = {}

server = d.ThreadingHTTPServer(("127.0.0.1", 0), d.Handler)
server.daemon_threads = True
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

def post(path, payload, timeout=10):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())

checkout_body = {"org_key": "t",
                 "org_config": {"name": "t", "good360_email": "x@x.test",
                                "good360_password": "pw",
                                "card": {"name": "T", "number": "4242424242424242",
                                         "expiry": "1229", "cvv": "123", "type": "visa"}},
                 "truck_name": "T", "truck_url": "http://x", "force_login": False}

# occupy the browser thread with the wedged checkout (returns 503 after 3s)
stuck = threading.Thread(target=post, args=("/test_checkout", checkout_body, 30), daemon=True)
stuck.start()
time.sleep(0.5)  # let the job land on the worker

t0 = time.time()
with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5) as r:
    health = json.loads(r.read())
health_ms = (time.time() - t0) * 1000
if health.get("status") != "ok":
    failures.append(f"/health wrong while browser wedged: {health}")
if health_ms > 2000:
    failures.append(f"/health took {health_ms:.0f}ms while browser wedged")

# a second checkout while wedged: fast 503, not a hang
t0 = time.time()
code, body = post("/test_checkout", checkout_body, timeout=10)
elapsed = time.time() - t0
if code != 503:
    failures.append(f"second checkout during wedge: HTTP {code} (expected 503) body={body}")
if elapsed > 8:
    failures.append(f"second checkout took {elapsed:.1f}s — hung instead of failing fast")

WEDGE.set()

# ---------------------------------------------------------------------------
# 3. v2 dispatch classification
# ---------------------------------------------------------------------------
import requests as real_requests
import good360_autobuy_v2 as v2

class _FakeReqs:
    exceptions = real_requests.exceptions
    @staticmethod
    def post(*a, **k):
        raise real_requests.exceptions.ConnectTimeout("daemon dead")

org = v2.OrgContext(org_id=1, org_uuid="u", org_name="T", contact_name="T",
                    contact_email="x@x", alert_email="", phone_number="",
                    sms_alerts_enabled=False, auto_buy_global=True,
                    master_card_fallback=False, max_price_override=None,
                    good360_email="x@x", good360_password="p", good360_org_id=None)
truck = v2.TruckContext(truck_event_id=1, truck_uuid="u", truck_title="T",
                        truck_url="http://x", truck_price=1.0, truck_location="",
                        truck_category="other", raw_data_json="{}")
card = {"card_number": "4242424242424242", "card_expiry_month": 12,
        "card_expiry_year": 2029, "card_cvv": "123", "card_type": "visa",
        "card_holder_name": "T", "billing_zip": "30046"}

sys.modules["requests"] = _FakeReqs()
try:
    res = v2._run_checkout_via_daemon(org, truck, card)
finally:
    sys.modules["requests"] = real_requests

if res.status != "daemon_unreachable":
    failures.append(f"ConnectTimeout classified as {res.status!r}, expected daemon_unreachable")
if "[DAEMON-UNREACHABLE]" not in (res.error_message or ""):
    failures.append(f"missing [DAEMON-UNREACHABLE] marker: {res.error_message}")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: port survives a wedged browser call (health instant, busy=503), "
      "worker deadlines fire, daemon-down classified as infra not card failure")
