import json
import os
import re
import smtplib
import subprocess
import sys
import traceback
from html import escape as html_escape
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Pull master scan credentials + OpenAI key from the dashboard's encrypted
# SQLite settings store. Single source of truth; the operator manages these
# via the dashboard UI. No-op if DASHBOARD_MASTER_KEY isn't set.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "good360_roster"))
import settings_bootstrap  # noqa: F401
try:
    import feature_flags
except ImportError:
    # Stale image/mount combo (old container recreated from a pinned compose
    # file without the feature_flags.py mount). Same semantics, env-only.
    import os as _ff_os
    import types as _ff_t

    def _ff_flag(name):
        return _ff_os.environ.get(name, "true").strip().lower() not in (
            "false", "0", "no", "off")

    feature_flags = _ff_t.SimpleNamespace(
        flag_enabled=lambda name, default=True: _ff_flag(name),
        auto_buy_enabled=lambda: _ff_flag("ENABLE_AUTO_BUY"),
        url_scanning_enabled=lambda: _ff_flag("ENABLE_URL_SCANNING"),
        notifications_enabled=lambda: _ff_flag("ENABLE_NOTIFICATIONS"),
        notifications_blocked_msg=lambda ch: (
            f"[NOTIFICATIONS DISABLED] {ch} send skipped "
            "(ENABLE_NOTIFICATIONS=false in this environment)"),
    )

ROSTER_ENABLED = os.environ.get("ROSTER_ENABLED", "false").strip().lower() in ("1", "true", "yes")
ROSTER_DRY_RUN = os.environ.get("ROSTER_DRY_RUN", "false").strip().lower() in ("1", "true", "yes")

import pytz
import requests
from playwright.sync_api import sync_playwright

# === TIMEZONE HELPER (Eastern Time) ===

ET = pytz.timezone("America/New_York")

def now_et():
    """Get current time in Eastern Time"""
    return datetime.now(ET)

def now_et_str():
    """Get current time in Eastern Time as formatted string"""
    return now_et().strftime("%Y-%m-%d %H:%M:%S")

import threading
import time
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================
import sandbox  # sandbox-mode URL/credential routing

# Live URL constants — sandbox mode swaps the host at call time via
# sandbox.good360_browse_url() / good360_login_url(). Kept as references so
# code that builds emails / log strings can show the live URL even while
# scanning the sandbox.
GOOD360_URL_LIVE = sandbox.LIVE_BROWSE_URL
GOOD360_LOGIN_URL_LIVE = sandbox.LIVE_LOGIN_URL

# Master scan credentials. Prefer SCAN_GOOD360_* (set by the dashboard),
# fall back to the legacy per-org name for backwards compatibility.
_LIVE_GOOD360_EMAIL = (os.environ.get("SCAN_GOOD360_EMAIL")
                       or os.environ.get("GOOD360_HOPE4HUMANITY_EMAIL", ""))
_LIVE_GOOD360_PASSWORD = (os.environ.get("SCAN_GOOD360_PASSWORD")
                          or os.environ.get("GOOD360_HOPE4HUMANITY_PASSWORD", ""))
GOOD360_EMAIL, GOOD360_PASSWORD = sandbox.scan_credentials(
    _LIVE_GOOD360_EMAIL, _LIVE_GOOD360_PASSWORD,
)

SMTP_USER = os.environ.get("ALERT_EMAIL_FROM", "")
SMTP_PASS = os.environ.get("SMTP_PASSWORD", "")
ALERT_TO = [e.strip() for e in os.environ.get("ALERT_EMAIL_TO", "").split(",") if e.strip()]


def _gmail_conn():
    # Submission port (587 + STARTTLS). The hosting provider blocks the
    # legacy SMTPS port (465), so SMTP_SSL fails with ENETUNREACH.
    server = smtplib.SMTP("smtp.gmail.com", 587, timeout=15)
    server.starttls()
    return server

# All outbound Telegram goes through the channel router (dashboard-managed
# registry in dashboard.db). The legacy fan-out to every org group is gone:
# admin/general messages hit operator channels, customer-attributable
# messages hit only that org's channel (falling back to admin).
import telegram_router

# ============================================================
# MULTI-ORG CONFIGURATION
# ============================================================
def load_orgs():
    """Load org configs from env-substituted template. Orgs w/ missing secrets are dropped."""
    try:
        from config import load_orgs as _load
        return _load()
    except Exception as e:
        log_cron(f"Warning: Could not load orgs config: {e}")
        return {}

def get_active_orgs_with_autobuy():
    """Get orgs that have auto-buy enabled and not paused/cooldown"""
    orgs = load_orgs()
    active = {}
    for key, org in orgs.items():
        if org.get("auto_buy") and not org.get("paused") and not org.get("cooldown_active"):
            active[key] = org
    return active

def get_org_for_truck(truck_name):
    """Find which org should auto-buy this truck based on targets"""
    active_orgs = get_active_orgs_with_autobuy()
    for key, org in active_orgs.items():
        targets = org.get("auto_buy_targets", [])
        for target in targets:
            if target.lower() in truck_name.lower():
                return key, org
    return None, None


def _categorize_truck(truck_name: str) -> str:
    """Map a truck title to the roster's category_key. Mirrors is_autobuy_target keywords."""
    n = truck_name.lower()
    if "new unsorted" in n or "unsorted" in n:
        return "unsorted"
    if "variety" in n:
        return "variety"
    if "houseware" in n:
        return "houseware"
    return "other"


def _roster_result_to_action(result, truck_name: str) -> str:
    """Translate handle_truck_event's return (dict or CheckoutResult) into an action_taken string."""
    # CheckoutResult dataclass path
    if hasattr(result, "success") and hasattr(result, "status"):
        org_label = f"org#{getattr(result, 'org_id', '?')}"
        if result.success:
            return f"AUTO-BUY SUCCESS ({org_label}) [roster]"
        status = (result.status or "unknown").upper()
        msg = result.error_message or ""
        return f"AUTO-BUY {status} ({org_label}) [roster]" + (f": {msg}" if msg else "")
    # Dict early-return path
    if isinstance(result, dict):
        s = result.get("status", "unknown")
        ev = result.get("event_id", "?")
        org = result.get("org_id")
        org_label = f"org#{org}" if org else "no-org"
        if s == "no_org_available":
            return f"AUTO-BUY SKIPPED [roster]: no available org in queue (event {ev})"
        if s == "category_excluded":
            return f"AUTO-BUY SKIPPED [roster]: category excluded for {org_label}"
        if s == "alert_only":
            return f"ALERT-ONLY [roster]: {org_label} is alert-only mode"
        if s == "price_exceeded":
            return f"AUTO-BUY SKIPPED [roster]: price exceeded for {org_label}"
        if s == "would_attempt_purchase":
            return f"DRY-RUN [roster]: would purchase via {org_label}"
        return f"AUTO-BUY {s.upper()} [roster] ({org_label})"
    return f"AUTO-BUY UNKNOWN [roster]: {result!r}"

TRACK_KEYWORDS = ["unsorted", "variety", "assorted"]
EXCLUDE_KEYWORDS = ["softline", "soft line", "softlines"]

WORKDIR = os.environ.get("WORKDIR", "/a0/usr/workdir")
APP_DIR = Path(__file__).resolve().parent
STATE_FILE = f"{WORKDIR}/good360_alerted_state.json"
LOG_FILE = f"{WORKDIR}/good360_run_log.json"
CRON_LOG_FILE = f"{WORKDIR}/good360_cron.log"
HEARTBEAT_FILE = f"{WORKDIR}/good360_heartbeat.json"
CONFIG_FILE = f"{WORKDIR}/good360_checkout_config.json"
AUTOBUY_SCRIPT = os.environ.get("AUTOBUY_SCRIPT", str(APP_DIR / "good360_autobuy.py"))
DEVTOOLS_AGENT_SCRIPT = os.environ.get("DEVTOOLS_AGENT_SCRIPT", str(APP_DIR / "good360_devtools_agent.py"))
AUTOBUY_ENGINE = os.environ.get("AUTOBUY_ENGINE", "daemon").strip().lower()

# ============================================================
# SINGLE-PURCHASE LOCK & COOLDOWN FUNCTIONS
# ============================================================
CONFIG_PATH = f"{WORKDIR}/good360_checkout_config.json"
LOCK_FILE = f"{WORKDIR}/good360_purchase_lock.flag"

def is_org_paused(org_name):
    """Check if org has auto-buy paused via status file."""
    status_file = f"{WORKDIR}/good360_org_status_{org_name}.json"
    try:
        with open(status_file) as f:
            status = json.load(f)
            return (status.get("paused", False), status.get("reason", ""))
    except FileNotFoundError:
        return (False, "")

def check_lock_and_cooldown():
    """Check if purchase lock exists or org is in cooldown period"""
    if os.path.exists(LOCK_FILE):
        return True, "LOCK_ACTIVE", "Another purchase in progress"
    try:
        with open(CONFIG_PATH) as f:
            config = json.load(f)
    except FileNotFoundError:
        # Config file is .gitignored and not always present (modern deploys read
        # creds + cooldown state from .env / dashboard settings). Treat absence
        # as "no cooldown" rather than logging it on every scan.
        return False, None, None
    except Exception as e:
        log_cron(f"  [COOLDOWN CHECK] Error: {e}")
        return False, None, None
    try:
        org = config.get("org_cooldown", {}).get("hope4humanity", {})
        if org.get("cooldown_active", False):
            next_allowed = org.get("next_allowed_date")
            if next_allowed and datetime.now(pytz.timezone("America/New_York")).date().isoformat() < next_allowed:
                return True, "COOLDOWN_ACTIVE", f"In cooldown until {next_allowed}"
    except Exception as e:
        log_cron(f"  [COOLDOWN CHECK] Error: {e}")
    return False, None, None

# AUTO-BUY: Always active during monitoring hours
MAX_AUTO_PAY = 6400.00

# Re-alert cadence: while a tracked truck STAYS available, repeat the
# availability alert this often (operator request 2026-06-12: never let a
# standing truck go quiet). First-detection alerts and the single-shot
# autobuy trigger are unaffected.
AVAILABLE_REALERT_SECONDS = int(os.environ.get("AVAILABLE_REALERT_SECONDS", "30"))


def _should_realert(state, name, now_ts):
    """True when a still-available truck's last alert is older than
    AVAILABLE_REALERT_SECONDS. Bookkeeping (writing the new timestamp)
    stays at the call site."""
    times = state.get("alert_times") or {}
    return (now_ts - times.get(name, 0)) >= AVAILABLE_REALERT_SECONDS

# ============================================================
# IGNORED PRODUCTS (user-managed via dashboard's Scans tab)
# ============================================================
# Dashboard writes a JSON list of truck names the operator has chosen to
# skip. We re-read on every check so the change is picked up without a
# monitor restart. Living in the shared workdir volume — same path the
# missioncontrol container writes to.
IGNORED_PRODUCTS_FILE = f"{WORKDIR}/ignored_products.json"


def load_ignored_products():
    """Return a set of truck names the user has marked ignored. Missing or
    malformed file = empty set (we never block on the ignore-list itself)."""
    try:
        with open(IGNORED_PRODUCTS_FILE) as f:
            data = json.load(f)
        return set(data) if isinstance(data, list) else set()
    except (FileNotFoundError, json.JSONDecodeError):
        return set()


def get_product_db_flags(name):
    """Return (tracked, autobuy_enabled) for a truck name from the dashboard
    DB, or (None, None) if the row doesn't exist yet. Caller falls back to
    the keyword defaults in that case so the first sighting still works."""
    try:
        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        from db import get_conn as _get_conn  # type: ignore
        with _get_conn() as c:
            row = c.execute(
                "SELECT tracked, autobuy_enabled FROM tracked_products WHERE name = ?",
                (name,),
            ).fetchone()
            if row is None:
                return None, None
            return int(row["tracked"]), int(row["autobuy_enabled"])
    except Exception:
        return None, None


_last_verifier_check = 0.0


def _maybe_run_order_verifier():
    """Dynamic buyer history: kick the daily Order History sync from the
    monitor's single process (gunicorn has 3 workers — running it there
    would stampede). Checked at most hourly here; run_full_sync() itself
    is 24h-gated and file-locked, so this is cheap and safe to call every
    scan."""
    global _last_verifier_check
    if time.time() - _last_verifier_check < 3600:
        return
    _last_verifier_check = time.time()
    try:
        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        import order_verifier
        _fire_and_forget("order-verifier", order_verifier.run_full_sync)
    except Exception as e:
        log_cron(f"  [ORDER VERIFIER] trigger failed: {e}")


def _autobuy_banner():
    """AUTO-BUY banner built from the live tracked_products toggles.

    The old banner hardcoded a product list ("New Unsorted + Variety +
    Houseware") that drifted from what the operator actually has toggled
    on in the dashboard — this reads the same table get_product_db_flags
    uses, so the banner can't lie."""
    cap = f"(max ${MAX_AUTO_PAY:,.0f})"
    try:
        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        from db import get_conn as _get_conn  # type: ignore
        with _get_conn() as c:
            rows = c.execute(
                "SELECT name FROM tracked_products "
                "WHERE tracked = 1 AND autobuy_enabled = 1 ORDER BY name"
            ).fetchall()
        names = [r["name"] for r in rows]
    except Exception:
        return f"  [AUTO-BUY: ACTIVE - product toggles unavailable {cap}]"
    if not names:
        return "  [AUTO-BUY: ACTIVE - but NO products have auto-buy enabled]"
    return f"  [AUTO-BUY: ACTIVE - Auto-buying: {' + '.join(names)} {cap}]"


def find_next_queued_customer(truck_price=None):
    """Return the next eligible nonprofit row from the QuickBeed-synced
    customers table, or None if nobody is waiting.

    Eligibility filter:
      - status = 'active' (QuickBeed account in good standing)
      - in_rotation = 1 (operator hasn't toggled them out locally)
      - cooldown_until is past (post-purchase rest period over)
      - max_budget >= truck_price (or both NULL)

    Ordering is round-robin-ish: least-recently-used first, then priority.
    Returns a plain dict so callers don't depend on sqlite3.Row's lifetime.
    """
    try:
        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        from db import get_conn as _get_conn  # type: ignore
        with _get_conn() as c:
            # Order: operator's drag-and-drop position first when set;
            # unranked rows fall back to LRU + priority_level. Cooldown rows
            # are excluded by the WHERE clause and re-enter at their manual
            # position once their cooldown clears.
            row = c.execute("""
                SELECT * FROM customers
                WHERE status = 'active'
                  AND in_rotation = 1
                  AND (cooldown_until IS NULL OR cooldown_until < datetime('now'))
                  AND (? IS NULL OR max_budget IS NULL OR max_budget >= ?)
                ORDER BY
                    (manual_queue_position IS NULL),
                    manual_queue_position ASC,
                    COALESCE(last_used_at, '1970-01-01') ASC,
                    CASE COALESCE(priority_level, 'normal')
                        WHEN 'high' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END
                LIMIT 1
            """, (truck_price, truck_price)).fetchone()
            return dict(row) if row else None
    except Exception as e:
        # Queue lookup failures must not crash the scan; we'd rather skip
        # autobuy than silently buy without a queued customer.
        log_cron(f"  [QUEUE] customers lookup failed: {e}")
        return None


def record_legacy_purchase_attempt(*, status, msg, details, truck_name, truck_url,
                                   truck_price=None, org_name=None, queued_customer=None):
    """Insert a row into dashboard.db legacy_purchase_attempts for every
    autobuy attempt — success or failure. Best-effort: a DB failure here
    must never break the scan loop.

    Captures enough context for the Purchases page to render the attempt
    AND for the operator to dig deeper (capture path, screenshot path,
    confirmation number, engine that ran).
    """
    try:
        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        from db import get_conn as _get_conn  # type: ignore
        d = details or {}
        with _get_conn() as c:
            c.execute("""
                INSERT INTO legacy_purchase_attempts
                  (status, engine, org_name, customer_id, customer_name,
                   truck_name, truck_url, truck_price, order_total,
                   confirmation_number, error_message,
                   capture_path, screenshot_path, elapsed_seconds)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                status,
                d.get("engine"),
                org_name,
                (queued_customer or {}).get("id"),
                (queued_customer or {}).get("organization_name"),
                truck_name,
                truck_url,
                truck_price,
                d.get("order_total"),
                d.get("confirmation_number"),
                msg if status not in ("SUCCESS",) else None,
                d.get("capture_path"),
                d.get("screenshot_path"),
                d.get("elapsed_seconds"),
            ))

            # Opportunistic price capture: every autobuy attempt is the one
            # place this account CAN see a real number (Good360 hides the
            # admin fee on listings but the cart/checkout page renders it).
            # Parse it out of order_total + msg + stdout and stamp it onto
            # tracked_products so the dashboard's Price column fills in
            # without manual entry for any product that's been attempted.
            captured = _extract_price_from_attempt(d, msg)
            if captured is not None:
                c.execute(
                    "UPDATE tracked_products "
                    "SET last_price = ?, updated_at = datetime('now') "
                    "WHERE name = ?",
                    (captured, truck_name),
                )
                log_cron(f"  [PRICE CAPTURE] {truck_name}: ${captured:.2f}")
    except Exception as e:
        log_cron(f"  [PURCHASE LOG] insert failed (non-fatal): {e}")


def _extract_price_from_attempt(details: dict, msg: str | None):
    """Pull a usable price out of an autobuy result. Preference order:
    explicit order_total → the largest $-amount in the textual message →
    the largest in the captured stdout tail. None if nothing parseable."""
    if details:
        ot = details.get("order_total")
        if isinstance(ot, (int, float)) and ot > 0:
            return float(ot)
    # Aggregate text from the message and the stdout tail captured by the
    # script-engine path. msg is usually one line ("Total $X exceeds limit
    # $Y"); stdout_tail is much richer.
    blob = " ".join(filter(None, [
        msg or "",
        (details or {}).get("stdout_tail") or "",
        (details or {}).get("stderr_tail") or "",
    ]))
    # Match $N,NNN.NN — admin fees and totals are always two-decimal.
    amounts = re.findall(r"\$\s*([\d,]+\.\d{2})", blob)
    if not amounts:
        return None
    # The script logs "Admin fee: $X, Shipping: $Y, Total: $Z" — Total is
    # always the largest, so picking the max gives us the truck's true cost.
    try:
        return max(float(a.replace(",", "")) for a in amounts)
    except ValueError:
        return None


def mark_customer_in_flight(customer_id, hold_minutes=10):
    """Reserve a customer the moment they're assigned a truck so the
    next iteration of find_next_queued_customer in this same scan picks
    a DIFFERENT customer. Sets a short cooldown (default 10 min) that
    auto-expires if the autobuy crashes without cleanup. mark_customer_
    assigned() overwrites this with the real outcome (success cooldown
    or release).
    """
    if not customer_id:
        return
    try:
        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        from db import get_conn as _get_conn  # type: ignore
        with _get_conn() as c:
            c.execute("""
                UPDATE customers
                SET last_used_at  = datetime('now'),
                    cooldown_until = datetime('now', '+' || ? || ' minutes')
                WHERE id = ?
            """, (int(hold_minutes), customer_id))
    except Exception as e:
        log_cron(f"  [QUEUE] in-flight reservation failed: {e}")


def mark_customer_assigned(customer_id, status, cooldown_days=7):
    """Release the in-flight reservation after an autobuy attempt completes.

    The roster engine does its own outcome bookkeeping for whichever org
    it ACTUALLY purchased for — including writing the real post-purchase
    cooldown to this table (_sync_cooldown_to_dashboard) — and the org it
    picks is not guaranteed to be the customer this monitor reserved. So
    this only ever clears SHORT holds (the ~10-min in-flight reservation
    from mark_customer_in_flight); a real multi-day cooldown is never
    touched, whatever the status says.
    """
    if not customer_id:
        return
    try:
        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        from db import get_conn as _get_conn  # type: ignore
        with _get_conn() as c:
            cur = c.execute("""
                UPDATE customers
                SET last_used_at = datetime('now'),
                    cooldown_until = NULL
                WHERE id = ?
                  AND cooldown_until IS NOT NULL
                  AND cooldown_until <= datetime('now', '+15 minutes')
            """, (customer_id,))
        if cur.rowcount:
            log_cron(f"  [QUEUE] in-flight reservation released for {customer_id} "
                     f"(outcome: {status})")
    except Exception as e:
        log_cron(f"  [QUEUE] customer bookkeeping failed: {e}")


def keyword_autobuy_default(name):
    """Default autobuy_enabled value for a brand-new product, used the first
    time we ever see a truck name before the operator has had a chance to
    toggle it. Mirrors the legacy `is_autobuy_target` keyword check so we
    don't regress existing behavior for "Variety / Unsorted / Houseware".
    """
    n = name.lower()
    if "softline" in n:
        return 0
    return 1 if ("new unsorted" in n or "variety" in n or "houseware" in n) else 0

# ============================================================
# LOGGING
# ============================================================
def log_cron(msg, print_it=True):
    """Log a message to the cron log file"""
    ts = now_et_str()  # Use Eastern Time
    line = f"[{ts}] {msg}"
    with open("good360_cron.log", "a") as f:
        f.write(line + "\n")
    if print_it:
        print(line)
def load_log():
    if not os.path.exists(LOG_FILE):
        return {"runs": []}
    try:
        with open(LOG_FILE) as f:
            data = f.read().strip()
            if not data:
                return {"runs": []}
            return json.loads(data)
    except (json.JSONDecodeError, ValueError) as e:
        print(f"Warning: Corrupted run log, resetting: {e}")
        try:
            import shutil
            shutil.copy2(LOG_FILE, LOG_FILE + ".corrupted")
        except: pass
        return {"runs": []}

def save_log(log):
    # Retain ~7 days of scan history at the current ~60s cadence so the
    # Analytics page has a useful window. The whole file is rewritten on
    # every scan, so bumping much higher than this is wasteful.
    log["runs"] = log["runs"][-10000:]
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

def append_run_log(timestamp, trucks, alert_sent, action=""):
    # Continue to update the legacy JSON file for back-compat with any code
    # that still reads it directly (the dashboard's analytics + scans
    # endpoints prefer the SQL `scans` table when present, and only fall
    # back to JSON if the SQL path returns nothing).
    log = load_log()
    if not isinstance(log, dict) or 'runs' not in log:
        print('⚠️ Detected malformed log structure — auto‑rebuilding good360_run_log.json')
        log = { 'runs': [] }
    truck_rows = [
        {
            "name":      t["name"],
            "available": t["available"],
            "tracked":   t["tracked"],
            "url":       t.get("url"),
            "price":     t.get("price"),
        }
        for t in trucks
    ]
    run_entry = {
        "time": timestamp,
        "alert_sent": alert_sent,
        "action": action,
        "trucks": truck_rows,
    }
    log["runs"].append(run_entry)
    save_log(log)

    # SQL mirror — durable, no rewrite-amplification, queryable for analytics.
    # Best-effort: a DB failure must never break the scan.
    try:
        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        from db import get_conn as _get_conn  # type: ignore
        avail = sum(1 for t in truck_rows if t.get("available"))
        with _get_conn() as _c:
            _c.execute(
                """INSERT INTO scans (ts, alert_sent, action, truck_count,
                                       available_count, trucks_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (str(timestamp), 1 if alert_sent else 0, action or "",
                 len(truck_rows), avail, json.dumps(truck_rows)),
            )
            # Upsert each observed truck into tracked_products. New names
            # land with tracked=1 (so they show in the dashboard right away)
            # and autobuy_enabled=0 (operator must opt in explicitly).
            for t in truck_rows:
                name = (t.get("name") or "").strip()
                if not name:
                    continue
                # New rows seed `autobuy_enabled` from the keyword default —
                # so the legacy Variety/Unsorted/Houseware behavior is
                # preserved on first sighting. After insert the operator
                # toggle is the authority (ON CONFLICT doesn't touch it).
                _ab_default = keyword_autobuy_default(name)
                _c.execute("""
                    INSERT INTO tracked_products
                        (name, tracked, autobuy_enabled, last_url, last_price,
                         first_seen, last_seen, updated_at)
                    VALUES (?, 1, ?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(name) DO UPDATE SET
                        last_url   = COALESCE(excluded.last_url,   last_url),
                        last_price = COALESCE(excluded.last_price, last_price),
                        last_seen  = excluded.last_seen,
                        updated_at = datetime('now')
                """, (name, _ab_default, t.get("url"), t.get("price"),
                      str(timestamp), str(timestamp)))
    except Exception as _exc:
        print(f"⚠️ SQL scan mirror failed (non-fatal): {_exc}")

# ============================================================
# WATCHDOG HEARTBEAT
# ============================================================
def write_heartbeat(status="running"):
    """Write heartbeat file with timestamp to indicate script is running"""
    heartbeat = {
        "last_success": datetime.now(pytz.timezone("America/New_York")).isoformat(),
        "script": "good360_monitor.py",
        "status": status
    }
    with open(HEARTBEAT_FILE, "w") as f:
        json.dump(heartbeat, f, indent=2)
    log_cron("Heartbeat written")

# ============================================================
# ERROR ALERTING
# ============================================================
def send_error_alert(error_message):
    """Send notification when script encounters errors"""
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("error-alert email"))
        return False
    timestamp = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    subject = sandbox.alert_prefix() + "Good360 Monitor ERROR Alert"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(ALERT_TO)

    html = f"""<html><body style='font-family:Arial,sans-serif;'>
<div style='background:#c0392b;padding:20px;border-radius:8px 8px 0 0;text-align:center;'>
<h2 style='color:white;margin:0;'>Good360 Monitor ERROR</h2>
<p style='color:#f39c12;margin:5px 0 0 0;font-weight:bold;'>SCRIPT ENCOUNTERED AN ERROR</p>
</div>
<div style='border:1px solid #ddd;border-top:none;padding:20px;border-radius:0 0 8px 8px;'>
<p><strong>Time:</strong> {timestamp}</p>
<p><strong>Error:</strong> <pre style='background:#f8f9fa;padding:10px;border-radius:4px;'>{error_message}</pre></p>
<p style='color:#e74c3c;font-weight:bold;'>The monitoring script needs attention!</p>
</div></body></html>"""

    msg.attach(MIMEText(html, "html"))
    try:
        with _gmail_conn() as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
        log_cron("Error alert email sent")
    except Exception as e:
        log_cron(f"Failed to send error alert email: {e}")

    tg_msg = sandbox.alert_prefix() + f"Good360 Monitor ERROR\n\nTime: {timestamp}\nError: {error_message}\n\nPlease check the script!\n- E-Comsetter Good360 Monitor"
    if telegram_router.send(telegram_router.ADMIN, tg_msg, source='monitor', level='error'):
        log_cron("Error alert Telegram sent")
    else:
        log_cron("Failed to send error alert Telegram (see notifications log)")

# ============================================================
# PRE-FLIGHT VALIDATION
# ============================================================
def pre_flight_validation():
    """Validate all required functions are defined before running"""
    required_functions = [
        "is_org_paused",
        "check_lock_and_cooldown",
        "load_state",
        "save_state",
        "is_autobuy_active",
        "send_telegram",
        "send_alert_email",
        "send_telegram_alert",
        "send_urgent_manual_alert",
        "send_purchase_confirmation",
        "run_autobuy",
        "check_trucks",
        "main",
        "log_cron",
        "write_heartbeat",
        "send_error_alert"
    ]

    missing = []
    for func in required_functions:
        if func not in globals():
            missing.append(func)

    if missing:
        error_msg = f"Pre-flight validation failed: Missing functions: {missing}"
        log_cron(error_msg)
        send_error_alert(error_msg)
        return False
    return True

# ============================================================
# HELPERS
# ============================================================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"alerted": []}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def is_autobuy_active():
    # Environment master switch: staging/feature set ENABLE_AUTO_BUY=false
    # so those stacks can never purchase, regardless of dashboard toggles.
    if not feature_flags.auto_buy_enabled():
        return False
    return True

def send_telegram(message):
    """Operator/admin Telegram via the channel router.

    The legacy body fanned this out to ALL org groups + the operator chat —
    which leaked one customer's data into other customers' chats. Customer-
    attributable messages must now go through
    telegram_router.send(telegram_router.NGO, ...) with the org; anything
    left calling this helper is admin-facing."""
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("telegram"))
        return False
    message = sandbox.decorate_alert(message)
    return telegram_router.send(telegram_router.ADMIN, message, source='monitor')

def _fire_and_forget(label, fn, *args, **kwargs):
    """Run a notification call on a daemon thread so the autobuy loop isn't
    blocked by SMTP/Telegram round-trips. Failures are logged, never raised —
    a missed alert must not delay or crash a buy attempt."""
    def _runner():
        try:
            fn(*args, **kwargs)
        except Exception as _exc:
            try:
                log_cron(f"async {label} failed: {_exc}")
            except Exception:
                pass
    threading.Thread(target=_runner, name=f"alert-{label}", daemon=True).start()


def send_alert_email(available_trucks, subject_prefix="ALERT", extra_note=""):
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("email"))
        return False
    msg = MIMEMultipart("alternative")
    msg["Subject"] = sandbox.alert_prefix() + subject_prefix + ": Amazon Truckload NOW AVAILABLE on Good360!"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(ALERT_TO)
    truck_rows = ""
    for t in available_trucks:
        truck_rows += "<tr><td style='padding:8px;border:1px solid #ddd;'>" + t["name"] + "</td>"
        truck_rows += "<td style='padding:8px;border:1px solid #ddd;color:green;font-weight:bold;'>AVAILABLE</td></tr>"
    extra_html = ""
    if extra_note:
        extra_html = "<p style='color:#e74c3c;font-weight:bold;'>" + extra_note + "</p>"
    now_str = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    html = """<html><body style='font-family:Arial,sans-serif;'>
<div style='background:#2c3e50;padding:20px;border-radius:8px 8px 0 0;text-align:center;'>
<h2 style='color:white;margin:0;'>E-Comsetter Good360 Monitor</h2>
<p style='color:#f39c12;margin:5px 0 0 0;font-weight:bold;'>TRUCK AVAILABLE ALERT!</p>
</div>
<div style='border:1px solid #ddd;border-top:none;padding:20px;border-radius:0 0 8px 8px;'>
""" + extra_html + """
<p>The following Amazon truckload(s) are now <strong>AVAILABLE</strong>. Act fast!</p>
<table style='border-collapse:collapse;width:100%;'>
<tr style='background:#27ae60;color:white;'>
<th style='padding:8px;border:1px solid #ddd;'>Truck</th>
<th style='padding:8px;border:1px solid #ddd;'>Status</th>
</tr>""" + truck_rows + """</table><br>
<p style='text-align:center;'><a href='""" + sandbox.good360_browse_url() + """'
style='background:#27ae60;color:white;padding:12px 24px;text-decoration:none;border-radius:5px;font-size:16px;'>VIEW &amp; ORDER NOW</a></p>
<br><p style='color:#888;font-size:12px;'>Detected at: """ + now_str + """ - E-Comsetter Good360 Monitor</p>
</div></body></html>"""
    msg.attach(MIMEText(html, "html"))
    with _gmail_conn() as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
    print("Alert email sent for: " + str([t["name"] for t in available_trucks]))



def send_telegram_alert(available_trucks, extra_note=""):
    """Truck availability alert — GENERAL channels (no customer data)."""
    now = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    truck_list = "\n".join(["- " + t["name"] for t in available_trucks])
    extra = "\n" + extra_note if extra_note else ""
    message = sandbox.alert_prefix() + "ALERT: Amazon Truckload AVAILABLE!\n\n" + truck_list + extra + "\n\nOrder NOW: " + sandbox.good360_browse_url() + "\n\nDetected: " + now + "\n- E-Comsetter Good360 Monitor"
    telegram_router.send(telegram_router.GENERAL, sandbox.decorate_alert(message), source='monitor')

def send_urgent_manual_alert(truck_name, admin_fee, truck_url, org_key=None):
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("urgent-manual alert"))
        return False
    now = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    subject = "URGENT ACTION REQUIRED - $" + str(admin_fee) + " Exceeds Auto-Pay Limit"
    html = """<html><body style='font-family:Arial,sans-serif;max-width:600px;margin:auto'>
<div style='background:#d93025;padding:20px;border-radius:8px 8px 0 0'>
<h2 style='color:white;margin:0'>URGENT - MANUAL PURCHASE REQUIRED</h2>
</div>
<div style='padding:20px;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px'>
<p style='font-size:18px;color:red'><strong>YOU MUST PURCHASE MANUALLY - ACT FAST!</strong></p>
<table style='width:100%;border-collapse:collapse;margin:15px 0'>
<tr><td style='padding:10px;border:1px solid #fcc'><strong>Truck</strong></td>
<td style='padding:10px;border:1px solid #fcc'>""" + truck_name + """</td></tr>
<tr><td style='padding:10px;border:1px solid #fcc'><strong>Admin Fee</strong></td>
<td style='padding:10px;border:1px solid #fcc;color:red'><strong>$""" + str(admin_fee) + """</strong></td></tr>
<tr><td style='padding:10px;border:1px solid #fcc'><strong>Auto-Pay Limit</strong></td>
<td style='padding:10px;border:1px solid #fcc'>$""" + f"{MAX_AUTO_PAY:,.0f}" + """</td></tr>
<tr><td style='padding:10px;border:1px solid #fcc'><strong>Detected At</strong></td>
<td style='padding:10px;border:1px solid #fcc'>""" + now + """</td></tr>
</table>
<a href='""" + truck_url + """' style='display:inline-block;background:#d93025;color:white;padding:12px 24px;border-radius:5px;text-decoration:none;font-size:16px'>GO BUY NOW MANUALLY</a>
<p style='color:#888;font-size:12px;margin-top:20px'>- E-Comsetter Good360 Monitor</p>
</div></body></html>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(ALERT_TO)
    msg.attach(MIMEText(html, "html"))
    with _gmail_conn() as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
    tg_msg = "URGENT - MANUAL PURCHASE REQUIRED\n\nAdmin fee exceeds auto-pay limit!\n\nTruck: " + truck_name + "\nAdmin Fee: $" + str(admin_fee) + f"\nAuto-Pay Limit: ${MAX_AUTO_PAY:,.0f}\nDetected: " + now + "\n\nGO BUY MANUALLY NOW:\n" + truck_url + "\n\n- E-Comsetter Good360 Monitor"
    telegram_router.send(telegram_router.NGO, sandbox.decorate_alert(tg_msg),
                         org_key=org_key, source='monitor', level='warn')
    print("URGENT manual alert sent - fee $" + str(admin_fee) + " exceeds limit")

def _redact_sensitive_text(text, org_config=None):
    """Redact known secrets before sending diagnostics to operators."""
    redacted = str(text or "")
    values = [
        GOOD360_PASSWORD,
        os.environ.get("CARD_HOPE4HUMANITY_NUMBER", ""),
        os.environ.get("CARD_HOPE4HUMANITY_CVV", ""),
        os.environ.get("CARD_REVIVING_HOMES_NUMBER", ""),
        os.environ.get("CARD_REVIVING_HOMES_CVV", ""),
    ]
    if org_config:
        values.extend([
            org_config.get("good360_password", ""),
            (org_config.get("card") or {}).get("number", ""),
            (org_config.get("card") or {}).get("cvv", ""),
        ])
    for value in values:
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    return redacted

def _details_lines(details, org_config=None):
    if not details:
        return []
    lines = []
    for key, value in details.items():
        if value in (None, "", [], {}):
            continue
        if isinstance(value, (list, tuple)):
            value = "; ".join(str(v) for v in value[:10])
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=True)
        value = _redact_sensitive_text(value, org_config)
        lines.append((str(key).replace("_", " ").title(), str(value)))
    return lines

def send_purchase_confirmation(truck_name, admin_fee, org_name=None, details=None, org_config=None, org_key=None):
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("purchase-confirmation alert"))
        return False
    now = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    subject = "AUTO-PURCHASE COMPLETE - " + truck_name
    org_line = f"<p><strong>Organization:</strong> {org_name}</p>" if org_name else ""
    detail_rows = ""
    for label, value in _details_lines(details or {}, org_config):
        detail_rows += "<tr><td style='padding:10px;border:1px solid #cfc'><strong>" + html_escape(label) + "</strong></td>"
        detail_rows += "<td style='padding:10px;border:1px solid #cfc'>" + html_escape(value) + "</td></tr>"
    html = """<html><body style='font-family:Arial,sans-serif;max-width:600px;margin:auto'>
<div style='background:#0f9d58;padding:20px;border-radius:8px 8px 0 0'>
<h2 style='color:white;margin:0'>AUTO-PURCHASE COMPLETE!</h2>
</div>
<div style='padding:20px;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px'>
<p style='font-size:16px'>Your E-Comsetter monitor has <strong>successfully purchased a truck!</strong></p>
""" + org_line + """
<table style='width:100%;border-collapse:collapse;margin:15px 0'>
<tr style='background:#f0fff4'><td style='padding:10px;border:1px solid #cfc'><strong>Truck</strong></td>
<td style='padding:10px;border:1px solid #cfc'>""" + truck_name + """</td></tr>
<tr><td style='padding:10px;border:1px solid #cfc'><strong>Admin Fee Paid</strong></td>
<td style='padding:10px;border:1px solid #cfc;color:green'><strong>$""" + str(admin_fee) + """</strong></td></tr>
<tr style='background:#f0fff4'><td style='padding:10px;border:1px solid #cfc'><strong>Purchased At</strong></td>
<td style='padding:10px;border:1px solid #cfc'>""" + now + """</td></tr>
<tr><td style='padding:10px;border:1px solid #cfc'><strong>Ship To</strong></td>
<td style='padding:10px;border:1px solid #cfc'>1025 Progress Circle, Lawrenceville GA 30043</td></tr>
""" + detail_rows + """
</table>
<p>Check your Good360 account for the order confirmation email.</p>
<p style='color:#888;font-size:12px;margin-top:20px'>- E-Comsetter Good360 Monitor</p>
</div></body></html>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(ALERT_TO)
    msg.attach(MIMEText(html, "html"))
    with _gmail_conn() as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
    org_text = ("\nOrganization: " + org_name) if org_name else ""
    detail_text = ""
    for label, value in _details_lines(details or {}, org_config):
        detail_text += "\n" + label + ": " + value
    tg_msg = "AUTO-PURCHASE COMPLETE!\n\nTruck: " + truck_name + org_text + "\nAdmin Fee: $" + str(admin_fee) + "\nShip To: 1025 Progress Circle, Lawrenceville GA 30043\nPurchased At: " + now + detail_text + "\n\nCheck Good360 email for confirmation!\n- E-Comsetter Good360 Monitor"
    telegram_router.send(telegram_router.NGO, sandbox.decorate_alert(tg_msg),
                         org_key=org_key, source='monitor', level='success')
    print("Purchase confirmation sent for: " + truck_name)

def send_checkout_failure_alert(truck_name, truck_url, status, message, org_name=None, details=None, org_config=None, org_key=None):
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("checkout-failure alert"))
        return False
    now = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    subject = "AUTO-PURCHASE FAILED - " + truck_name
    safe_message = _redact_sensitive_text(message, org_config)
    org_line = f"<p><strong>Organization:</strong> {html_escape(org_name)}</p>" if org_name else ""
    detail_rows = ""
    for label, value in _details_lines(details or {}, org_config):
        detail_rows += "<tr><td style='padding:10px;border:1px solid #fcc'><strong>" + html_escape(label) + "</strong></td>"
        detail_rows += "<td style='padding:10px;border:1px solid #fcc'>" + html_escape(value) + "</td></tr>"
    html = """<html><body style='font-family:Arial,sans-serif;max-width:700px;margin:auto'>
<div style='background:#d93025;padding:20px;border-radius:8px 8px 0 0'>
<h2 style='color:white;margin:0'>AUTO-PURCHASE FAILED</h2>
</div>
<div style='padding:20px;border:1px solid #ddd;border-top:none;border-radius:0 0 8px 8px'>
<p style='font-size:16px;color:#d93025'><strong>The truck was detected, but checkout did not complete.</strong></p>
""" + org_line + """
<table style='width:100%;border-collapse:collapse;margin:15px 0'>
<tr><td style='padding:10px;border:1px solid #fcc'><strong>Truck</strong></td>
<td style='padding:10px;border:1px solid #fcc'>""" + html_escape(truck_name) + """</td></tr>
<tr><td style='padding:10px;border:1px solid #fcc'><strong>Status</strong></td>
<td style='padding:10px;border:1px solid #fcc'>""" + html_escape(status) + """</td></tr>
<tr><td style='padding:10px;border:1px solid #fcc'><strong>Reason</strong></td>
<td style='padding:10px;border:1px solid #fcc'>""" + html_escape(safe_message) + """</td></tr>
<tr><td style='padding:10px;border:1px solid #fcc'><strong>Time</strong></td>
<td style='padding:10px;border:1px solid #fcc'>""" + html_escape(now) + """</td></tr>
""" + detail_rows + """
</table>
<p><a href='""" + html_escape(truck_url) + """' style='display:inline-block;background:#d93025;color:white;padding:12px 24px;border-radius:5px;text-decoration:none'>Open Truck Manually</a></p>
<p style='color:#888;font-size:12px;margin-top:20px'>Sensitive values are redacted before notification.</p>
</div></body></html>"""
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(ALERT_TO)
    msg.attach(MIMEText(html, "html"))
    try:
        with _gmail_conn() as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
    except Exception as e:
        log_cron(f"Failed to send checkout failure email: {e}")

    org_text = ("\nOrganization: " + org_name) if org_name else ""
    detail_text = ""
    for label, value in _details_lines(details or {}, org_config):
        detail_text += "\n" + label + ": " + value
    tg_msg = "AUTO-PURCHASE FAILED\n\nTruck: " + truck_name + org_text + "\nStatus: " + status + "\nReason: " + safe_message + "\nDetected: " + now + detail_text + "\n\nManual link:\n" + truck_url + "\n\n- E-Comsetter Good360 Monitor"
    telegram_router.send(telegram_router.NGO, sandbox.decorate_alert(tg_msg),
                         org_key=org_key, source='monitor', level='error')
    # Purchase failures are customer-specific issues the system channel
    # must also carry (operator directive 2026-07-08). Mirror to ADMIN —
    # unless the NGO send already fell back there (no channel for the org).
    if telegram_router.resolve_channels(telegram_router.NGO,
                                        org_key=org_key, fallback=False):
        telegram_router.send(telegram_router.ADMIN, sandbox.decorate_alert(tg_msg),
                             source='monitor', level='error')
    print("Checkout failure alert sent for: " + truck_name)


# ============================================================
# PERSISTENT BROWSER DAEMON INTEGRATION
# ============================================================
import urllib.request as _urlreq

# In Docker Compose the daemon is reachable via its service hostname, not
# localhost — each container has its own loopback. Default to the compose
# service name; allow override for bare-metal or alternate topologies.
DAEMON_URL = os.environ.get("DAEMON_URL", "http://daemon:5002")
DAEMON_TIMEOUT = 45  # seconds

def try_daemon_checkout(truck_name, truck_url, org_key):
    """Try fast checkout via persistent browser daemon.
    Returns (status, msg, elapsed, capture_path) or None if daemon unavailable.
    """
    try:
        payload = json.dumps({
            "org_key": org_key,
            "truck_name": truck_name,
            "truck_url": truck_url
        }).encode('utf-8')
        req = _urlreq.Request(
            f"{DAEMON_URL}/checkout",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        resp = _urlreq.urlopen(req, timeout=DAEMON_TIMEOUT)
        result = json.loads(resp.read().decode('utf-8'))
        status = result.get("status", "ERROR")
        msg = result.get("message", "No message")
        elapsed = result.get("elapsed", 0)
        capture_path = result.get("capture_path")
        print(f"  [DAEMON] {status} in {elapsed}s: {msg}")
        return status, msg, elapsed, capture_path
    except Exception as e:
        print(f"  [DAEMON] Unavailable: {e}")
        return None

def check_daemon_health():
    """Check if daemon is running and healthy."""
    try:
        req = _urlreq.Request(f"{DAEMON_URL}/health", method="GET")
        resp = _urlreq.urlopen(req, timeout=3)
        data = json.loads(resp.read().decode('utf-8'))
        return data.get("status") == "ok"
    except:
        return False

def _invoke_devtools_agent(truck_name, truck_url, admin_fee, org_key):
    """Run the Chrome DevTools MCP checkout agent as a subprocess.

    Returns (status, message, details). The caller decides whether to use the
    result or fall through. Network errors, missing deps, and parse failures
    all collapse into a FAILED return — never raises.
    """
    if not org_key:
        return "FAILED", "DevTools agent requires org_key", {"engine": "devtools_agent"}
    try:
        print("  [DEVTOOLS AGENT] Attempting Chrome DevTools MCP checkout...")
        cmd_args = [
            sys.executable,
            DEVTOOLS_AGENT_SCRIPT,
            truck_name,
            truck_url,
            str(admin_fee),
            org_key,
        ]
        if os.environ.get("DEVTOOLS_AGENT_DRY_RUN", "").lower() in ("1", "true", "yes", "on"):
            cmd_args.append("--dry-run")
        result = subprocess.run(
            cmd_args,
            capture_output=True,
            text=True,
            timeout=int(os.environ.get("DEVTOOLS_AGENT_TIMEOUT_SECONDS") or "420"),
        )
        raw = result.stdout.strip()
        print("  [DEVTOOLS AGENT] output: " + raw[-1000:])
        try:
            payload = json.loads(raw[raw.find("{"):])
            status = payload.get("status", "FAILED")
            msg = payload.get("message", "No message")
            details = {
                "engine": "devtools_agent",
                "order_total": payload.get("order_total"),
                "confirmation_number": payload.get("confirmation_number"),
                "final_url": payload.get("final_url"),
                "evidence": payload.get("evidence", []),
            }
        except Exception:
            status = "FAILED"
            msg = raw.splitlines()[-1] if raw else result.stderr[-500:] or "No output"
            details = {
                "engine": "devtools_agent",
                "exit_code": result.returncode,
                "stderr": result.stderr[-1000:],
                "stdout_tail": raw[-1000:],
            }
        return status, msg, details
    except subprocess.TimeoutExpired:
        return "FAILED", "DevTools agent timed out", {"engine": "devtools_agent", "error": "timeout"}
    except Exception as e:
        return "FAILED", str(e), {"engine": "devtools_agent", "error": str(e)}


def _devtools_agent_available() -> bool:
    """Cheap pre-flight: only attempt the agent if its safety + auth env are
    in place. Otherwise the subprocess would either refuse to start or send a
    silent prompt with no auth — better to skip than spam OpenAI calls that
    can't succeed."""
    if not os.environ.get("OPENAI_API_KEY"):
        return False
    if os.environ.get("DEVTOOLS_AGENT_ALLOW_LIVE_PURCHASE", "").lower() not in ("1", "true", "yes", "on"):
        return False
    return True


def run_autobuy(truck_name, truck_url, admin_fee, org_key=None, org_config=None):
    """Try configured checkout engines. Returns (status, message, details).

    Engine ladder:
      1. DevTools agent (only if explicitly selected via AUTOBUY_ENGINE)
      2. Daemon (fast, persistent browser)
      3. Script (slow, full Playwright cold start)
      4. DevTools agent (escalation, on script FAILED, if API key + safety
         flag are set and agent hasn't already been tried at step 1)

    SUCCESS / MISSED / MANUAL / COOLDOWN / LOCKED / BLOCKED / DRY_RUN are
    terminal — only FAILED triggers the next rung. Step 4 turns the agent
    into a self-healing fallback for when our deterministic selectors break
    on a Good360 DOM change.
    """
    # Hard environment gate, independent of is_autobuy_active(): staging and
    # feature stacks must never reach any checkout engine.
    if not feature_flags.auto_buy_enabled():
        msg = "Auto-buy disabled in this environment (ENABLE_AUTO_BUY=false)"
        print("  [BLOCKED] " + msg)
        return ("BLOCKED", msg, {"reason": "ENABLE_AUTO_BUY=false"})

    print("  Launching auto-buy for: " + truck_name)
    if org_key:
        org_name = org_config.get('name', org_key) if org_config else org_key
        print(f"  Using org: {org_key} ({org_name})")

    agent_already_tried = False
    devtools_fallback_on_failed = os.environ.get(
        "DEVTOOLS_AGENT_FALLBACK_ON_FAILED", ""
    ).lower() in ("1", "true", "yes", "on")

    # ===== Step 1 (opt-in primary): AI checkout via Chrome DevTools MCP =====
    if AUTOBUY_ENGINE in ("devtools_agent", "chrome_devtools_mcp", "mcp"):
        status, msg, details = _invoke_devtools_agent(truck_name, truck_url, admin_fee, org_key)
        agent_already_tried = True
        if status in ("SUCCESS", "MISSED", "MANUAL", "DRY_RUN", "BLOCKED"):
            return status, msg, details
        if devtools_fallback_on_failed:
            print(f"  [DEVTOOLS AGENT] Failed ({msg}) - falling back to daemon/script...")
        else:
            return "FAILED", msg, details

    # ===== Step 2: persistent browser daemon (fast, <5s) =====
    if check_daemon_health():
        print("  [DAEMON] Persistent browser available - attempting fast checkout...")
        daemon_result = try_daemon_checkout(truck_name, truck_url, org_key)
        if daemon_result is not None:
            daemon_status, daemon_msg, daemon_elapsed, daemon_capture = daemon_result
            if daemon_status in ("SUCCESS", "MISSED"):
                return daemon_status, daemon_msg, {
                    "engine": "daemon",
                    "elapsed_seconds": daemon_elapsed,
                    "capture_path": daemon_capture,
                }
            if daemon_status == "FAILED":
                print(f"  [DAEMON] Failed ({daemon_msg}) - falling back to script...")
            else:
                return daemon_status, daemon_msg, {
                    "engine": "daemon",
                    "elapsed_seconds": daemon_elapsed,
                    "capture_path": daemon_capture,
                }
    else:
        print("  [DAEMON] Not available - using script fallback...")

    # ===== Step 3: Playwright script (slow, ~30s, full deterministic flow) =====
    script_status, script_msg, script_details = None, None, None
    try:
        print("  [SCRIPT] Running fallback auto-buy script...")
        cmd_args = [sys.executable, AUTOBUY_SCRIPT, truck_name, truck_url, str(admin_fee)]
        if org_key:
            cmd_args.append(org_key)
        # Pass the QuickBeed customer UUID via env so autobuy can pull
        # THIS customer's partner credentials from missioncontrol (and
        # not fall back to the master scan account). The id flows in
        # from the queue manager via org_config; absent in legacy paths.
        sub_env = os.environ.copy()
        cid = (org_config or {}).get("quickbeed_customer_id") or (org_config or {}).get("customer_id")
        if cid:
            sub_env["GOOD360_CUSTOMER_ID"] = str(cid)
        result = subprocess.run(
            cmd_args,
            capture_output=True, text=True, timeout=300,
            env=sub_env,
        )
        print("  Auto-buy output: " + result.stdout[-500:])
        exit_code_map = {
            0: "SUCCESS",
            1: "FAILED",
            2: "MISSED",
            3: "MANUAL",
            4: "COOLDOWN",
            5: "LOCKED",
            6: "CARD_DECLINED",
        }
        script_status = exit_code_map.get(result.returncode, "ERROR")
        msg_lines = result.stdout.strip().split("\n")
        script_msg = msg_lines[-1] if msg_lines else "No message"
        print(f"  Auto-buy result: {script_status} - {script_msg}")
        # The autobuy script prints "[CAPTURE] /app/workdir/checkout_captures/…json"
        # via atexit. Pull that out so the dashboard can link to the capture
        # JSON for this attempt. Workdir-relative path is what the file
        # endpoint expects.
        capture_path = None
        for ln in result.stdout.splitlines():
            ln = ln.strip()
            if ln.startswith("[CAPTURE] "):
                full = ln[len("[CAPTURE] "):].strip()
                workdir = os.environ.get("WORKDIR", "/app/workdir")
                if full.startswith(workdir + "/"):
                    capture_path = full[len(workdir) + 1:]
                else:
                    capture_path = full
        script_details = {
            "engine": "script",
            "exit_code": result.returncode,
            "stdout_tail": result.stdout[-1500:],
            "stderr_tail": result.stderr[-1500:],
            "capture_path": capture_path,
        }
    except subprocess.TimeoutExpired:
        print("  Auto-buy timed out after 5 minutes")
        script_status, script_msg = "FAILED", "Timeout after 5 minutes"
        script_details = {"engine": "script", "error": "timeout"}
    except Exception as e:
        print(f"  Auto-buy error: {e}")
        script_status, script_msg = "FAILED", str(e)
        script_details = {"engine": "script", "error": str(e)}

    # ===== Step 4: escalation to DevTools agent on FAILED =====
    # Only fires when the deterministic script truly failed — SUCCESS/MISSED/
    # MANUAL/COOLDOWN/LOCKED/ERROR all bypass this. Avoids re-running the agent
    # if it was already the primary engine on this call.
    if (
        script_status == "FAILED"
        and not agent_already_tried
        and _devtools_agent_available()
    ):
        print("  [ESCALATE] Script FAILED — handing off to DevTools MCP agent...")
        agent_status, agent_msg, agent_details = _invoke_devtools_agent(
            truck_name, truck_url, admin_fee, org_key
        )
        agent_details = agent_details or {}
        agent_details["escalated_from_script"] = True
        agent_details["script_failure"] = script_msg
        return agent_status, agent_msg, agent_details

    return script_status, script_msg, script_details
def check_trucks():
    # Best-effort import of the login telemetry recorder. If the dashboard
    # modules aren't reachable (e.g., legacy deploy), keep working anyway.
    _record_login = None
    try:
        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        from login_telemetry import record_login_attempt as _record_login
    except Exception:
        _record_login = None

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        # Login: capture pass/fail telemetry so the dashboard's Login panel
        # can show what email tried and whether it worked.
        _login_started = time.time()
        _login_ok = False
        _login_err = None
        # Try the login up to 2 times. The first failure is almost always
        # Good360's analytics stack (GA / livehelpnow / trackedweb / newrelic)
        # leaving the page in a state where `networkidle` never resolves and
        # the email field hasn't been painted by the time wait_for_selector
        # gives up. A single retry catches those — one in maybe-ten scans.
        # We deliberately do NOT use `networkidle` anywhere on this site for
        # the same reason — those analytics keep firing forever.
        _LOGIN_ATTEMPTS = 2
        _SELECTOR_TIMEOUT_MS = 30_000      # was 15s — bumped for slow SPAs
        _GOTO_TIMEOUT_MS = 45_000          # was 30s
        _POST_SUBMIT_TIMEOUT_MS = 20_000   # was 15s

        _login_url = sandbox.good360_autobuy_login_url()
        try:
            for _attempt in range(1, _LOGIN_ATTEMPTS + 1):
                try:
                    # `domcontentloaded` (not networkidle) — the SPA bundle
                    # is loaded but analytics keep the network busy
                    # indefinitely. wait_for_selector below is what actually
                    # confirms the form is interactable.
                    page.goto(_login_url, wait_until="domcontentloaded",
                              timeout=_GOTO_TIMEOUT_MS)
                    # `:visible` matters — the live page has a hidden Sign-up
                    # form input with identical name+placeholder.
                    page.wait_for_selector(
                        'input[placeholder*="email" i]:visible',
                        state="visible", timeout=_SELECTOR_TIMEOUT_MS,
                    )
                    page.fill('input[placeholder*="email" i]:visible', GOOD360_EMAIL)
                    page.fill('input[placeholder*="password" i]:visible', GOOD360_PASSWORD)
                    # Enter submits the form past the cookie banner that
                    # would otherwise eat a button click.
                    try:
                        page.press('input[placeholder*="password" i]:visible', "Enter")
                    except Exception:
                        page.click(
                            'button[type="submit"]:has-text("Sign in"):visible',
                            timeout=10_000, force=True,
                        )
                    # Don't wait for networkidle (same reason as above).
                    # `load` resolves once the navigation finishes — enough
                    # for the post-login truck listing to start rendering.
                    page.wait_for_load_state("load", timeout=_POST_SUBMIT_TIMEOUT_MS)
                    _login_ok = True
                    break
                except Exception as _exc:
                    if _attempt >= _LOGIN_ATTEMPTS:
                        raise
                    _login_err = f"{type(_exc).__name__}: {_exc}"
                    print(f"  [LOGIN] attempt {_attempt} failed ({type(_exc).__name__}); retrying once...")
                    # Drop the page and rebuild — the existing tab may be
                    # stuck on a partially-loaded state that won't recover.
                    try:
                        page.close()
                    except Exception:
                        pass
                    page = context.new_page()
                    time.sleep(1.5)
        except Exception as _exc:
            _login_err = f"{type(_exc).__name__}: {_exc}"
            if _record_login:
                try:
                    _record_login(source="monitor", email=GOOD360_EMAIL,
                                  success=False, error=_login_err,
                                  duration_ms=int((time.time() - _login_started) * 1000))
                except Exception:
                    pass
            raise
        if _record_login:
            try:
                _record_login(source="monitor", email=GOOD360_EMAIL,
                              success=True, error=None,
                              duration_ms=int((time.time() - _login_started) * 1000))
            except Exception:
                pass

        page.goto(sandbox.good360_browse_url(), wait_until="networkidle", timeout=30000)
        # Previously slept 2s here "to let listings finish rendering." networkidle
        # has already waited for the SPA to settle, so this was pure dead time
        # on every scan — removing it shaves ~2s off the 10s cadence budget.
        truck_links = {}
        try:
            anchors = page.query_selector_all("a")
            for anchor in anchors:
                try:
                    text = anchor.inner_text().strip()
                    href = anchor.get_attribute("href") or ""
                    if "amazon" in text.lower() and "truckload" in text.lower() and href:
                        full_url = href if href.startswith("http") else sandbox.good360_base_url() + href
                        truck_links[text[:50]] = full_url
                except:
                    continue
        except Exception as e:
            print("Link extraction error: " + str(e))
        content = page.inner_text("body")
        browser.close()
    lines = content.split("\n")
    current_product = None
    # Listing cards interleave the product name, an admin-fee dollar amount,
    # and an Available/Not available indicator. We keep the most-recent $-
    # amount seen between the name and the availability line as the truck's
    # current price. Compare-at and other prices come AFTER availability
    # so they don't pollute this value.
    current_price = None
    price_re = re.compile(r"\$\s*([\d,]+(?:\.\d{1,2})?)")
    for line in lines:
        line = line.strip()
        if "Amazon" in line and ("Truckload" in line or "truckload" in line) and "from Amazon" not in line and "Browse" not in line:
            current_product = line
            current_price = None
        elif current_product:
            m = price_re.search(line)
            if m:
                try:
                    current_price = float(m.group(1).replace(",", ""))
                except ValueError:
                    pass
            if "Available" in line or "Not available" in line:
                name = current_product
                is_available = "Not available" not in line and "Available" in line
                name_lower = name.lower()
                should_track = any(kw in name_lower for kw in TRACK_KEYWORDS)
                excluded = any(kw in name_lower for kw in EXCLUDE_KEYWORDS)
                truck_url = sandbox.good360_browse_url()
                for link_text, link_url in truck_links.items():
                    if link_text.lower()[:20] in name.lower() or name.lower()[:20] in link_text.lower():
                        truck_url = link_url
                        break
                results.append({
                    "name": name,
                    "available": is_available,
                    "tracked": should_track and not excluded,
                    "url": truck_url,
                    "price": current_price,
                })
                current_product = None
                current_price = None
    return results

# ============================================================
# MAIN
# ============================================================
def main():
    """Main monitoring logic with comprehensive error handling"""
    now = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    print("[" + now + "] Checking Good360 Amazon truckloads...")
    log_cron(f"Starting check at {now}")

    # Pre-flight validation
    if not pre_flight_validation():
        return "Pre-flight validation failed"

    try:
        autobuy_active = is_autobuy_active()
        if autobuy_active:
            print(_autobuy_banner())
            log_cron("Auto-buy active")
        else:
            print("  [AUTO-BUY: INACTIVE - alert only mode]")
            log_cron("Auto-buy inactive")

        state = load_state()
        trucks = check_trucks()

        print("Found " + str(len(trucks)) + " trucks:")
        for t in trucks:
            status = "AVAILABLE" if t["available"] else "Not available"
            # --- Enhanced truck classification rules ---
            # The operator's Tracked toggle (Mission Control → Scans →
            # Observed products) is authoritative in BOTH directions: it can
            # rescue a truck the keyword rules exclude (e.g. Softlines) and
            # silence one they track. The keyword rules below only decide
            # trucks that have no dashboard row yet (first sighting).
            name_lower = t['name'].lower()
            db_tracked, _db_autobuy_unused = get_product_db_flags(t['name'])
            if db_tracked is not None:
                if bool(db_tracked) != t['tracked']:
                    log_cron(f'  [OPERATOR OVERRIDE] dashboard toggle wins for '
                             f'{t["name"]}: tracked={"on" if db_tracked else "off"}')
                t['tracked'] = bool(db_tracked)
            elif 'softline' in name_lower:
                t['tracked'] = False  # exclude softline by default
                log_cron(f'  [EXCLUDED] Softline truck skipped: {t["name"]}')
            elif 'fulfillment center' in name_lower:
                t['tracked'] = True
                t['priority'] = 'autobuy'
                log_cron(f'  [AUTO-BUY TARGET] Fulfillment Center truck: {t["name"]}')
            elif any(x in name_lower for x in [' fc ', ' nc ']):
                t['tracked'] = True
                t['priority'] = 'alert'
                log_cron(f'  [ALERT-ONLY] FC/NC type truck tracked: {t["name"]}')
            # keep existing Unsorted / Variety / Houseware tracking
            # -----------------------------------------------------
            tracked = "[TRACKED]" if t["tracked"] else "[skipped]"
            print("  " + tracked + " " + t["name"] + " -> " + status)
            if t["tracked"] and t["available"]:
                # IMMEDIATE ALERT: Send the INSTANT truck is detected.
                # Fire-and-forget — the autobuy attempt that follows must not
                # wait on SMTP/Telegram round-trips. Trucks sell out in <2s;
                # blocking here used to add ~3s before checkout even began.
                if t["name"] not in state.get("alerted", []):
                    print(f"  [IMMEDIATE ALERT] {t['name']} - dispatching async alerts")
                    _fire_and_forget("immediate-email", send_alert_email, [t],
                        extra_note="TRUCK DETECTED - Alert sent the INSTANT truck was found! This is an IMMEDIATE alert sent before any auto-buy processing.")
                    _fire_and_forget("immediate-telegram", send_telegram_alert, [t],
                        extra_note="IMMEDIATE ALERT - Truck detected! Alert sent FIRST before auto-buy.")
                    log_cron(f"IMMEDIATE ALERT dispatched for {t['name']}")
                    state.setdefault("alert_times", {})[t["name"]] = time.time()
                elif _should_realert(state, t["name"], time.time()):
                    # RE-ALERT: repeat every AVAILABLE_REALERT_SECONDS while
                    # the truck stays available so the operator can't miss
                    # it. Autobuy is NOT re-triggered here — it stays
                    # single-shot per availability (see skill §2 re-arm).
                    print(f"  [RE-ALERT] {t['name']} still available - re-dispatching alerts")
                    _fire_and_forget("realert-email", send_alert_email, [t],
                        extra_note="REMINDER: truck is STILL AVAILABLE.")
                    _fire_and_forget("realert-telegram", send_telegram_alert, [t],
                        extra_note="REMINDER - truck still available!")
                    log_cron(f"RE-ALERT dispatched for {t['name']}")
                    state.setdefault("alert_times", {})[t["name"]] = time.time()

                log_cron(f"TRACKED TRUCK AVAILABLE: {t['name']}")

        # Defensive guard: ensure 'alerted' is always a list, not bool or other type
        if not isinstance(state.get("alerted"), list):
            state["alerted"] = []

        newly_available = [
            t for t in trucks
            if t["tracked"] and t["available"] and t["name"] not in state["alerted"]
        ]

        still_available_names = [t["name"] for t in trucks if t["available"]]
        state["alerted"] = [name for name in state["alerted"] if name in still_available_names]
        # Prune re-alert timestamps the same way so a truck that disappears
        # and comes back gets a fresh immediate alert, not a stale clock.
        if not isinstance(state.get("alert_times"), dict):
            state["alert_times"] = {}
        state["alert_times"] = {k: v for k, v in state["alert_times"].items()
                                if k in still_available_names}

        alert_sent = False
        action_taken = ""

        if newly_available:
            print("NEW trucks available: " + str([t["name"] for t in newly_available]))
            log_cron(f"New trucks available: {[t['name'] for t in newly_available]}")

            for truck in newly_available:
                truck_name = truck["name"]
                truck_url = truck.get("url", sandbox.good360_browse_url())

                # 1. ALERT-FIRST: dispatch the availability alert asynchronously
                # so autobuy can race to the checkout immediately. SMTP +
                # Telegram round-trips were adding ~2-3s between truck-detect
                # and checkout-start; trucks sell out in <2s, so that delay
                # was directly causing MISSED outcomes.
                print(f"  [ALERT-FIRST] dispatching availability alert for {truck_name}")
                _fire_and_forget("availability-email", send_alert_email, [truck],
                    extra_note="Truck is available. Auto-buy will be attempted if applicable.")
                _fire_and_forget("availability-telegram", send_telegram_alert, [truck],
                    extra_note="Truck available. Auto-buy will be attempted if applicable.")
                log_cron(f"Alert dispatched for {truck_name}")

                # Per-product flags from tracked_products (Scans → Observed
                # Products). Tracked is the single off-switch: if the
                # operator has turned it off, skip BOTH alert echoes and
                # autobuy. Otherwise autobuy_enabled gates the buy step;
                # new names fall back to the legacy keyword default so the
                # first scan still acts on Variety/Unsorted/Houseware
                # without manual ticking.
                truck_lower = truck_name.lower()
                _db_tracked, _db_autobuy = get_product_db_flags(truck_name)

                if _db_tracked == 0:
                    print(f"  [UNTRACKED] {truck_name} — tracked=off, no alert echo, no autobuy")
                    log_cron(f"Untracked (operator off): {truck_name}")
                    action_taken = "UNTRACKED - SKIPPED"
                    if truck_name not in state["alerted"]:
                        state["alerted"].append(truck_name)
                    alert_sent = True
                    continue

                if _db_autobuy is None:
                    is_autobuy_target = bool(keyword_autobuy_default(truck_name))
                else:
                    is_autobuy_target = bool(_db_autobuy)

                # Queue gate: even when autobuy is enabled for a product, we
                # only attempt a purchase if there's a real nonprofit waiting
                # for it. No queued customer → alert-only. Prevents the
                # legacy "fire-and-forget against whichever org sat first in
                # the JSON" behavior the operator flagged as unsafe.
                _truck_price_for_queue = truck.get("price") if isinstance(truck, dict) else None
                queued_customer = find_next_queued_customer(_truck_price_for_queue) if is_autobuy_target else None
                if is_autobuy_target and not queued_customer:
                    print(f"  [QUEUE EMPTY] No nonprofit in queue for {truck_name} - alert only, no autobuy")
                    log_cron(f"Autobuy skipped (queue empty) for {truck_name}")
                    is_autobuy_target = False
                    action_taken = "QUEUE EMPTY - ALERT ONLY"
                elif queued_customer:
                    print(f"  [QUEUE ASSIGN] {queued_customer.get('organization_name')!r} (id={queued_customer.get('id')}) queued to receive {truck_name}")
                    log_cron(f"Autobuy assignment: {queued_customer.get('organization_name')} -> {truck_name}")
                    # Reserve the customer immediately so the next truck in
                    # this same scan picks a DIFFERENT customer. Without
                    # this, two near-simultaneous availabilities both get
                    # routed to the same nonprofit because cooldown_until
                    # isn't written until after the autobuy completes.
                    mark_customer_in_flight(queued_customer.get('id'))

                if is_autobuy_target and ROSTER_ENABLED:
                    # NEW PATH: route through roster_orchestrator → QuickBeed customer rotation.
                    # Picks next eligible QB customer from roster.db, autobuy_v2 fetches creds
                    # live from QuickBeed, applies cooldown, dispatches notifications internally.
                    try:
                        from roster_orchestrator import handle_truck_event, log_truck_event
                        event_id = log_truck_event(
                            truck_title=truck_name,
                            truck_url=truck_url,
                            truck_price=0.0,
                            truck_location=truck.get("location", ""),
                            truck_category=_categorize_truck(truck_name),
                            raw_data=truck,
                        )
                        log_cron(f"Roster event #{event_id} logged for {truck_name}")
                        result = handle_truck_event(event_id, auto_run=not ROSTER_DRY_RUN)
                        action_taken = _roster_result_to_action(result, truck_name)
                        log_cron(f"Roster outcome for {truck_name}: {action_taken}")
                    except Exception as e:
                        err = f"Roster path failed for {truck_name}: {e}\n{traceback.format_exc()}"
                        print(err)
                        log_cron(err)
                        send_error_alert(err)
                        action_taken = f"ROSTER ERROR: {e}"
                    # Release the in-flight reservation taken at QUEUE ASSIGN.
                    # The roster engine bookkeeps the org it actually bought
                    # for (incl. the dashboard cooldown write-back); this only
                    # clears the short hold and never a real cooldown.
                    mark_customer_assigned(queued_customer.get('id'), action_taken)
                    if truck_name not in state["alerted"]:
                        state["alerted"].append(truck_name)
                    alert_sent = True
                    continue

                if is_autobuy_target:
                    # Load all orgs and find one that targets this truck AND has auto-buy active
                    all_orgs = load_orgs()
                    matching_org_key = None
                    matching_org = None

                    for org_key, org_data in all_orgs.items():
                        # Check if this org targets this truck
                        targets = org_data.get("auto_buy_targets", [])
                        targets_match = any(t.lower() in truck_lower for t in targets)

                        # Check if auto-buy is active for this org
                        auto_buy_on = org_data.get("auto_buy", False)
                        paused = org_data.get("paused", False)
                        cooldown = org_data.get("cooldown_active", False)

                        if targets_match and auto_buy_on and not paused and not cooldown:
                            matching_org_key = org_key
                            matching_org = org_data
                            break

                    if not matching_org_key:
                        # Check if any org targets this but is paused/cooldown
                        paused_reason = ""
                        for org_key, org_data in all_orgs.items():
                            targets = org_data.get("auto_buy_targets", [])
                            targets_match = any(t.lower() in truck_lower for t in targets)
                            if targets_match:
                                if org_data.get("paused", False):
                                    paused_reason = f"{org_data['name']} is PAUSED"
                                elif org_data.get("cooldown_active", False):
                                    paused_reason = f"{org_data['name']} is in COOLDOWN (until {org_data.get('cooldown_until', '?')})"
                                break

                        if paused_reason:
                            print(f"  [AUTO-BUY SKIPPED] {truck_name} - {paused_reason}")
                            action_taken = f"{paused_reason} - ALERT SENT"
                        else:
                            print(f"  [ALERT ONLY] {truck_name} - no org configured for auto-buy")
                            action_taken = "ALERT SENT"
                        log_cron(f"Auto-buy skipped for {truck_name}: {paused_reason or 'no org configured'}")
                        continue

                    # Found active org - attempt purchase
                    org_name = matching_org["name"]
                    print(f"  [AUTO-BUY ORG] {org_name} -> {truck_name}")
                    log_cron(f"Auto-buy initiated: {org_name} -> {truck_name}")

                    # Check lock/cooldown for this org
                    is_locked, lock_status, lock_msg = check_lock_and_cooldown()
                    if is_locked:
                        print(f"  [AUTO-BUY BLOCKED] {truck_name} - {lock_status}: {lock_msg}")
                        log_cron(f"Auto-buy blocked for {truck_name}: {lock_status}")
                        if lock_status == "COOLDOWN_ACTIVE":
                            action_taken = "COOLDOWN - ALERT SENT"
                        else:
                            print("  [AUTO-BUY SKIPPED] Another purchase in progress")
                            action_taken = "LOCKED - SKIPPED"
                        continue

                    # Run auto-buy with org-specific credentials
                    print(f"  [AUTO-BUY TARGET] {truck_name} -> {org_name} - initiating purchase...")
                    log_cron(f"Starting auto-buy for {truck_name} (org: {org_name})")
                    status, msg, checkout_details = run_autobuy(
                        truck_name,
                        truck_url,
                        0.0,
                        org_key=matching_org_key,
                        org_config=matching_org,
                    )

                    # Log EVERY attempt — success, failure, missed, manual,
                    # whatever. The Purchases page UNIONs this with the
                    # roster.db source so operators see every attempt.
                    record_legacy_purchase_attempt(
                        status=status,
                        msg=msg,
                        details=checkout_details or {},
                        truck_name=truck_name,
                        truck_url=truck_url,
                        truck_price=truck.get("price") if isinstance(truck, dict) else None,
                        org_name=org_name,
                        queued_customer=queued_customer,
                    )

                    if status == "SUCCESS":
                        send_purchase_confirmation(
                            truck_name,
                            checkout_details.get("order_total") or 0.0,
                            org_name=org_name,
                            details=checkout_details,
                            org_config=matching_org,
                            org_key=matching_org_key,
                        )
                        action_taken = f"AUTO-BUY SUCCESS ({org_name})"
                        log_cron(f"Auto-buy success for {truck_name} ({org_name})")
                        # Cooldown the assigned nonprofit so they don't get
                        # the next available truck right after this one.
                        if queued_customer:
                            mark_customer_assigned(queued_customer.get('id'), 'SUCCESS')
                            log_cron(f"Cooldown applied to {queued_customer.get('organization_name')} ({queued_customer.get('id')})")
                    elif status == "MISSED":
                        action_taken = f"AUTO-BUY MISSED ({org_name}) - ALERT SENT"
                        log_cron(f"Auto-buy missed for {truck_name} ({org_name}): {msg}")
                    elif status == "MANUAL":
                        # MANUAL means the truck total exceeded the auto-buy
                        # cap — it's an "act fast, buy by hand" notice, not a
                        # failure. Route to the urgent-manual alert instead of
                        # the generic checkout-failure path.
                        m = re.search(r"\$([\d,]+(?:\.\d+)?)", msg or "")
                        total_val = float(m.group(1).replace(",", "")) if m else 0.0
                        send_urgent_manual_alert(truck_name, total_val, truck_url,
                                                 org_key=matching_org_key)
                        action_taken = f"MANUAL REQUIRED ({org_name}): {msg}"
                        log_cron(f"Manual purchase required for {truck_name} ({org_name}): {msg}")
                    elif status == "COOLDOWN":
                        print(f"  [AUTO-BUY] Cooldown active: {msg}")
                        action_taken = f"COOLDOWN ({org_name}) - SKIPPED"
                        log_cron(f"Auto-buy cooldown for {truck_name} ({org_name}): {msg}")
                    elif status == "LOCKED":
                        print(f"  [AUTO-BUY] Lock active: {msg}")
                        action_taken = f"LOCKED ({org_name}) - SKIPPED"
                        log_cron(f"Auto-buy locked for {truck_name} ({org_name}): {msg}")
                    elif status == "DRY_RUN":
                        action_taken = f"DRY RUN ({org_name}) - NO PURCHASE"
                        log_cron(f"DevTools agent dry-run for {truck_name} ({org_name}): {msg}")
                    elif status == "BLOCKED":
                        send_checkout_failure_alert(
                            truck_name,
                            truck_url,
                            status,
                            msg,
                            org_name=org_name,
                            details=checkout_details,
                            org_config=matching_org,
                            org_key=matching_org_key,
                        )
                        action_taken = f"BLOCKED ({org_name}) - ALERT SENT"
                        log_cron(f"DevTools agent blocked purchase for {truck_name} ({org_name}): {msg}")
                    else:
                        send_checkout_failure_alert(
                            truck_name,
                            truck_url,
                            status,
                            msg,
                            org_name=org_name,
                            details=checkout_details,
                            org_config=matching_org,
                            org_key=matching_org_key,
                        )
                        action_taken = f"AUTO-BUY FAILED ({org_name}, {status}) - ALERT SENT"
                        log_cron(f"Auto-buy failed for {truck_name} ({org_name}): {status} - {msg}")

                    # Whatever the outcome — if it WASN'T a SUCCESS — clear
                    # the in-flight reservation so the customer is eligible
                    # again on the next attempt instead of sitting in a
                    # 10-minute hold for nothing.
                    if queued_customer and status != "SUCCESS":
                        mark_customer_assigned(queued_customer.get('id'), status)
                else:
                    print("  [ALERT ONLY] " + truck_name)
                    action_taken = "ALERT SENT"
                    log_cron(f"Alert-only for {truck_name} (not auto-buy target)")

                if truck_name not in state["alerted"]:
                    state["alerted"].append(truck_name)
                alert_sent = True

        save_state(state)
        append_run_log(now, trucks, alert_sent, action_taken)
        _maybe_run_order_verifier()

        if alert_sent:
            result_msg = action_taken + ": " + str([t["name"] for t in newly_available])
            log_cron(f"Check complete with alerts: {result_msg}")
            write_heartbeat()
            return result_msg
        else:
            print("No new available tracked trucks found.")
            log_cron("Check complete - no new trucks")
            write_heartbeat()
            return "No new available trucks"

    except Exception as e:
        error_msg = f"CRITICAL ERROR in main(): {str(e)}\nTraceback: {traceback.format_exc()}"
        print(error_msg)
        log_cron(error_msg)
        send_error_alert(error_msg)
        try:
            if 'trucks' in locals():
                available_trucks = [t for t in trucks if t['available'] and t['tracked']]
                if available_trucks:
                    log_cron(f"ERROR occurred but tracked trucks were available: {[t['name'] for t in available_trucks]}")
        except:
            pass
        return f"ERROR: {str(e)}"

if __name__ == "__main__":
    # Boot probe — confirm we can write the immutable audit log. A silent
    # stderr fallback (the current behavior when the workdir is unwritable)
    # means we lose the system-of-record for "did we actually buy that?"
    # Keep running either way — losing the audit shouldn't pause sales — but
    # alert loudly so an operator notices.
    try:
        from audit_log import verify_writable as _audit_verify
        _ok, _err = _audit_verify()
        if not _ok:
            warn = f"⚠️ AUDIT LOG NOT WRITABLE — purchase events will only hit stderr.\n\nReason: {_err}\n\nFix the workdir permission ASAP."
            print(f"[MONITOR] {warn}")
            try:
                send_telegram(warn)
            except Exception:
                pass
    except Exception as _probe_err:
        print(f"[MONITOR] Audit probe itself errored: {_probe_err}")

    # Run as a long-lived process: one Chromium boot, repeated scans on a
    # fixed interval. Previously this script exited after each scan and relied
    # on `restart: always` to re-launch — which cold-started Playwright ~880
    # times/day and made real crashes indistinguishable from normal cycling.
    # 10s default — minimizes detection latency for new truck availability.
    # Trucks sell out in <2s, so each second of scan latency directly costs
    # purchases. Override via env if Good360 ever returns a rate-limit response.
    SCAN_INTERVAL = int(os.environ.get("MONITOR_INTERVAL_SECONDS") or "10")
    _scan_disabled_logged = False
    while True:
        try:
            if not feature_flags.url_scanning_enabled():
                # Staging/feature environments run with ENABLE_URL_SCANNING=false:
                # never touch the live Good360 site, but keep the heartbeat
                # fresh so the container healthcheck stays green.
                if not _scan_disabled_logged:
                    print("[SCANNING DISABLED - ENABLE_URL_SCANNING=false] "
                          "Idling; no Good360 requests will be made")
                    _scan_disabled_logged = True
                write_heartbeat(status="scanning_disabled")
                time.sleep(SCAN_INTERVAL)
                continue
            result = main()
            print("Result: " + result)
        except KeyboardInterrupt:
            print("[MONITOR] Interrupted, exiting")
            break
        except Exception as e:
            # main() already catches and reports its own errors; this is the
            # last-resort guard so a bug here can't kill the loop silently.
            print(f"[MONITOR] Unhandled error in scan loop: {e}")
            traceback.print_exc()
        time.sleep(SCAN_INTERVAL)
