import json
import os
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

import time
from pathlib import Path

# ============================================================
# CONFIGURATION
# ============================================================
GOOD360_URL = "https://catalog.good360.org/marketplace/browse-goods/truckload-donations/amazon.html"
GOOD360_LOGIN_URL = "https://catalog.good360.org/marketplace/home"
# Master scan credentials. Prefer SCAN_GOOD360_* (set by the dashboard),
# fall back to the legacy per-org name for backwards compatibility.
GOOD360_EMAIL = (os.environ.get("SCAN_GOOD360_EMAIL")
                 or os.environ.get("GOOD360_HOPE4HUMANITY_EMAIL", ""))
GOOD360_PASSWORD = (os.environ.get("SCAN_GOOD360_PASSWORD")
                    or os.environ.get("GOOD360_HOPE4HUMANITY_PASSWORD", ""))

SMTP_USER = os.environ.get("ALERT_EMAIL_FROM", "")
SMTP_PASS = os.environ.get("SMTP_PASSWORD", "")
ALERT_TO = [e.strip() for e in os.environ.get("ALERT_EMAIL_TO", "").split(",") if e.strip()]

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Multi-org Telegram groups (values from .env)
ORG_TELEGRAM_GROUPS = {
    k: v for k, v in {
        "hope4humanity": os.environ.get("TELEGRAM_GROUP_HOPE4HUMANITY", ""),
        "reviving_homes": os.environ.get("TELEGRAM_GROUP_REVIVING_HOMES", ""),
    }.items() if v
}
ALL_TELEGRAM_GROUPS = list(ORG_TELEGRAM_GROUPS.values())

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

def get_all_org_telegram_groups():
    """Get list of all org Telegram groups for alerts"""
    orgs = load_orgs()
    groups = []
    for key, org in orgs.items():
        gid = org.get("telegram_group") or org.get("telegram_group_id")
        if gid:
            groups.append(gid)
    return groups

# Legacy single-chat fallback (prefer ORG_TELEGRAM_GROUPS)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_GROUP_HOPE4HUMANITY", "")

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
    log["runs"] = log["runs"][-200:]
    with open(LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

def append_run_log(timestamp, trucks, alert_sent, action=""):
    log = load_log()
    # Safeguard: if log structure invalid, rebuild it
    if not isinstance(log, dict) or 'runs' not in log:
        print('⚠️ Detected malformed log structure — auto‑rebuilding good360_run_log.json')
        log = { 'runs': [] }
    run_entry = {
        "time": timestamp,
        "alert_sent": alert_sent,
        "action": action,
        "trucks": [
            {"name": t["name"], "available": t["available"], "tracked": t["tracked"]}
            for t in trucks
        ]
    }
    log["runs"].append(run_entry)
    save_log(log)

# ============================================================
# WATCHDOG HEARTBEAT
# ============================================================
def write_heartbeat():
    """Write heartbeat file with timestamp to indicate script is running"""
    heartbeat = {
        "last_success": datetime.now(pytz.timezone("America/New_York")).isoformat(),
        "script": "good360_monitor.py",
        "status": "running"
    }
    with open(HEARTBEAT_FILE, "w") as f:
        json.dump(heartbeat, f, indent=2)
    log_cron("Heartbeat written")

# ============================================================
# ERROR ALERTING
# ============================================================
def send_error_alert(error_message):
    """Send notification when script encounters errors"""
    timestamp = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    subject = "Good360 Monitor ERROR Alert"

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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
        log_cron("Error alert email sent")
    except Exception as e:
        log_cron(f"Failed to send error alert email: {e}")

    tg_msg = f"Good360 Monitor ERROR\n\nTime: {timestamp}\nError: {error_message}\n\nPlease check the script!\n- E-Comsetter Good360 Monitor"
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": tg_msg,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        requests.post(url, json=payload, timeout=10)
        log_cron("Error alert Telegram sent")
    except Exception as e:
        log_cron(f"Failed to send error alert Telegram: {e}")

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
    return True

def send_telegram(message):
    """Send Telegram message to ALL org groups"""
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    for chat_id in ALL_TELEGRAM_GROUPS:
        try:
            payload = {
                "chat_id": chat_id,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True
            }
            response = requests.post(url, json=payload, timeout=10)
            result = response.json()
            if result.get("ok"):
                print(f"Telegram alert sent to {chat_id}!")
            else:
                print(f"Telegram error for {chat_id}: {result}")
        except Exception as e:
            print(f"Telegram failed for {chat_id}: {e}")

def send_alert_email(available_trucks, subject_prefix="ALERT", extra_note=""):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject_prefix + ": Amazon Truckload NOW AVAILABLE on Good360!"
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
<p style='text-align:center;'><a href='https://catalog.good360.org/marketplace/browse-goods/truckload-donations/amazon.html'
style='background:#27ae60;color:white;padding:12px 24px;text-decoration:none;border-radius:5px;font-size:16px;'>VIEW &amp; ORDER NOW</a></p>
<br><p style='color:#888;font-size:12px;'>Detected at: """ + now_str + """ - E-Comsetter Good360 Monitor</p>
</div></body></html>"""
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
    print("Alert email sent for: " + str([t["name"] for t in available_trucks]))



def load_all_orgs():
    """Load all active org configs from intake_form/submissions or org JSON files"""
    orgs = {}
    # Load from org config files in workdir
    org_files = [f for f in os.listdir(WORKDIR) if f.endswith("_config.json") and "good360" not in f and "checkout" not in f]
    for fname in org_files:
        try:
            with open(os.path.join(WORKDIR, fname)) as f:
                cfg = json.load(f)
            if cfg.get("status") == "active":
                key = fname.replace("_config.json", "")
                orgs[key] = cfg
        except Exception as e:
            log_cron(f"Warning: Could not load {fname}: {e}")
    return orgs

def get_org_telegram_group(org_key):
    """Get Telegram group for a specific org"""
    orgs = load_all_orgs()
    if org_key in orgs:
        return orgs[org_key].get("telegram_group_id", ORG_TELEGRAM_GROUPS.get(org_key, TELEGRAM_CHAT_ID))
    return ORG_TELEGRAM_GROUPS.get(org_key, TELEGRAM_CHAT_ID)

def send_telegram_to_org(org_key, message):
    """Send Telegram to specific org's group"""
    chat_id = get_org_telegram_group(org_key)
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
        result = requests.post(url, json=payload, timeout=10).json()
        if result.get("ok"):
            log_cron(f"Telegram alert sent to {org_key} ({chat_id})!")
        else:
            log_cron(f"Telegram error for {org_key} ({chat_id}): {result}")
    except Exception as e:
        log_cron(f"Telegram failed for {org_key} ({chat_id}): {e}")

def send_telegram_alert(available_trucks, extra_note=""):
    now = datetime.now(pytz.timezone("America/New_York")).strftime("%Y-%m-%d %H:%M:%S")
    truck_list = "\n".join(["- " + t["name"] for t in available_trucks])
    extra = "\n" + extra_note if extra_note else ""
    message = "ALERT: Amazon Truckload AVAILABLE!\n\n" + truck_list + extra + "\n\nOrder NOW: " + GOOD360_URL + "\n\nDetected: " + now + "\n- E-Comsetter Good360 Monitor"
    send_telegram(message)

def send_urgent_manual_alert(truck_name, admin_fee, truck_url):
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
<td style='padding:10px;border:1px solid #fcc'>$5,500</td></tr>
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
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
    tg_msg = "URGENT - MANUAL PURCHASE REQUIRED\n\nAdmin fee exceeds auto-pay limit!\n\nTruck: " + truck_name + "\nAdmin Fee: $" + str(admin_fee) + "\nAuto-Pay Limit: $5,500\nDetected: " + now + "\n\nGO BUY MANUALLY NOW:\n" + truck_url + "\n\n- E-Comsetter Good360 Monitor"
    send_telegram(tg_msg)
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

def send_purchase_confirmation(truck_name, admin_fee, org_name=None, details=None, org_config=None):
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
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
    org_text = ("\nOrganization: " + org_name) if org_name else ""
    detail_text = ""
    for label, value in _details_lines(details or {}, org_config):
        detail_text += "\n" + label + ": " + value
    tg_msg = "AUTO-PURCHASE COMPLETE!\n\nTruck: " + truck_name + org_text + "\nAdmin Fee: $" + str(admin_fee) + "\nShip To: 1025 Progress Circle, Lawrenceville GA 30043\nPurchased At: " + now + detail_text + "\n\nCheck Good360 email for confirmation!\n- E-Comsetter Good360 Monitor"
    send_telegram(tg_msg)
    print("Purchase confirmation sent for: " + truck_name)

def send_checkout_failure_alert(truck_name, truck_url, status, message, org_name=None, details=None, org_config=None):
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
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
    except Exception as e:
        log_cron(f"Failed to send checkout failure email: {e}")

    org_text = ("\nOrganization: " + org_name) if org_name else ""
    detail_text = ""
    for label, value in _details_lines(details or {}, org_config):
        detail_text += "\n" + label + ": " + value
    tg_msg = "AUTO-PURCHASE FAILED\n\nTruck: " + truck_name + org_text + "\nStatus: " + status + "\nReason: " + safe_message + "\nDetected: " + now + detail_text + "\n\nManual link:\n" + truck_url + "\n\n- E-Comsetter Good360 Monitor"
    send_telegram(tg_msg)
    print("Checkout failure alert sent for: " + truck_name)


# ============================================================
# PERSISTENT BROWSER DAEMON INTEGRATION
# ============================================================
import urllib.request as _urlreq

DAEMON_URL = "http://localhost:5002"
DAEMON_TIMEOUT = 45  # seconds

def try_daemon_checkout(truck_name, truck_url, org_key):
    """Try fast checkout via persistent browser daemon. Returns (status, msg, elapsed) or None if daemon unavailable."""
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
        print(f"  [DAEMON] {status} in {elapsed}s: {msg}")
        return status, msg, elapsed
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

def run_autobuy(truck_name, truck_url, admin_fee, org_key=None, org_config=None):
    """Try configured checkout engines. Returns (status, message, details)."""
    print("  Launching auto-buy for: " + truck_name)
    if org_key:
        org_name = org_config.get('name', org_key) if org_config else org_key
        print(f"  Using org: {org_key} ({org_name})")

    # ===== OPTIONAL: AI checkout via Chrome DevTools MCP =====
    # This engine is opt-in because it sends checkout context to an LLM-backed
    # agent and can perform live purchases when the explicit env guards allow it.
    if AUTOBUY_ENGINE in ("devtools_agent", "chrome_devtools_mcp", "mcp"):
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
                timeout=int(os.environ.get("DEVTOOLS_AGENT_TIMEOUT_SECONDS", "420")),
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

            if status in ("SUCCESS", "MISSED", "MANUAL", "DRY_RUN", "BLOCKED"):
                return status, msg, details
            if os.environ.get("DEVTOOLS_AGENT_FALLBACK_ON_FAILED", "").lower() in ("1", "true", "yes", "on"):
                print(f"  [DEVTOOLS AGENT] Failed ({msg}) - falling back to daemon/script...")
            else:
                return "FAILED", msg, details
        except subprocess.TimeoutExpired:
            if os.environ.get("DEVTOOLS_AGENT_FALLBACK_ON_FAILED", "").lower() in ("1", "true", "yes", "on"):
                print("  [DEVTOOLS AGENT] Timed out - falling back to daemon/script...")
            else:
                return "FAILED", "DevTools agent timed out", {"engine": "devtools_agent", "error": "timeout"}
        except Exception as e:
            if os.environ.get("DEVTOOLS_AGENT_FALLBACK_ON_FAILED", "").lower() in ("1", "true", "yes", "on"):
                print(f"  [DEVTOOLS AGENT] Error ({e}) - falling back to daemon/script...")
            else:
                return "FAILED", str(e), {"engine": "devtools_agent", "error": str(e)}

    # ===== PRIMARY: Try persistent browser daemon (fast - <5s) =====
    if check_daemon_health():
        print("  [DAEMON] Persistent browser available - attempting fast checkout...")
        daemon_result = try_daemon_checkout(truck_name, truck_url, org_key)
        if daemon_result is not None:
            daemon_status, daemon_msg, daemon_elapsed = daemon_result
            # If daemon succeeded or truck was missed (sold out), use that result
            if daemon_status in ("SUCCESS", "MISSED"):
                return daemon_status, daemon_msg, {
                    "engine": "daemon",
                    "elapsed_seconds": daemon_elapsed,
                }
            # If daemon failed, try fallback
            if daemon_status == "FAILED":
                print(f"  [DAEMON] Failed ({daemon_msg}) - falling back to script...")
            else:
                return daemon_status, daemon_msg, {
                    "engine": "daemon",
                    "elapsed_seconds": daemon_elapsed,
                }
    else:
        print("  [DAEMON] Not available - using script fallback...")

    # ===== FALLBACK: Run Playwright script (slow - ~30s) =====
    try:
        print("  [SCRIPT] Running fallback auto-buy script...")
        cmd_args = [sys.executable, AUTOBUY_SCRIPT, truck_name, truck_url, str(admin_fee)]
        if org_key:
            cmd_args.append(org_key)
        result = subprocess.run(
            cmd_args,
            capture_output=True, text=True, timeout=300
        )
        print("  Auto-buy output: " + result.stdout[-500:])
        exit_code_map = {
            0: "SUCCESS",
            1: "FAILED",
            2: "MISSED",
            3: "MANUAL",
            4: "COOLDOWN",
            5: "LOCKED"
        }
        status = exit_code_map.get(result.returncode, "ERROR")
        msg_lines = result.stdout.strip().split("\n")
        msg = msg_lines[-1] if msg_lines else "No message"
        print(f"  Auto-buy result: {status} - {msg}")
        return status, msg, {
            "engine": "script",
            "exit_code": result.returncode,
            "stdout_tail": result.stdout[-1500:],
            "stderr_tail": result.stderr[-1500:],
        }
    except subprocess.TimeoutExpired:
        print("  Auto-buy timed out after 5 minutes")
        return "FAILED", "Timeout after 5 minutes", {"engine": "script", "error": "timeout"}
    except Exception as e:
        print(f"  Auto-buy error: {e}")
        return "FAILED", str(e), {"engine": "script", "error": str(e)}
def check_trucks():
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.goto(GOOD360_LOGIN_URL, wait_until="networkidle", timeout=30000)
        page.click("text=Login", timeout=10000)
        page.wait_for_selector('input[placeholder*="email" i]', state="visible", timeout=15000)
        page.fill('input[placeholder*="email" i]', GOOD360_EMAIL)
        page.fill('input[placeholder*="password" i]', GOOD360_PASSWORD)
        page.click('button:has-text("Sign in")', timeout=10000)
        page.wait_for_load_state("networkidle", timeout=15000)
        page.goto(GOOD360_URL, wait_until="networkidle", timeout=30000)
        time.sleep(2)
        truck_links = {}
        try:
            anchors = page.query_selector_all("a")
            for anchor in anchors:
                try:
                    text = anchor.inner_text().strip()
                    href = anchor.get_attribute("href") or ""
                    if "amazon" in text.lower() and "truckload" in text.lower() and href:
                        full_url = href if href.startswith("http") else "https://catalog.good360.org" + href
                        truck_links[text[:50]] = full_url
                except:
                    continue
        except Exception as e:
            print("Link extraction error: " + str(e))
        content = page.inner_text("body")
        browser.close()
    lines = content.split("\n")
    current_product = None
    for line in lines:
        line = line.strip()
        if "Amazon" in line and ("Truckload" in line or "truckload" in line) and "from Amazon" not in line and "Browse" not in line:
            current_product = line
        elif current_product and ("Available" in line or "Not available" in line):
            name = current_product
            is_available = "Not available" not in line and "Available" in line
            name_lower = name.lower()
            should_track = any(kw in name_lower for kw in TRACK_KEYWORDS)
            excluded = any(kw in name_lower for kw in EXCLUDE_KEYWORDS)
            truck_url = GOOD360_URL
            for link_text, link_url in truck_links.items():
                if link_text.lower()[:20] in name.lower() or name.lower()[:20] in link_text.lower():
                    truck_url = link_url
                    break
            results.append({
                "name": name,
                "available": is_available,
                "tracked": should_track and not excluded,
                "url": truck_url
            })
            current_product = None
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
            print("  [AUTO-BUY: ACTIVE - Auto-buying: New Unsorted + Variety + Houseware Truckloads (max $6,400)]")
            log_cron("Auto-buy active")
        else:
            print("  [AUTO-BUY: INACTIVE - alert only mode]")
            log_cron("Auto-buy inactive")

        state = load_state()
        trucks = check_trucks()

        print("Found " + str(len(trucks)) + " trucks:")
        for t in trucks:
            status = "AVAILABLE" if t["available"] else "Not available"
            tracked = "[TRACKED]" if t["tracked"] else "[skipped]"
            print("  " + tracked + " " + t["name"] + " -> " + status)
            # --- Enhanced truck classification rules ---
            name_lower = t['name'].lower()
            if 'softline' in name_lower:
                t['tracked'] = False  # exclude softline
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
            if t["tracked"] and t["available"]:
                # IMMEDIATE ALERT: Send the INSTANT truck is detected
                if t["name"] not in state.get("alerted", []):
                    print(f"  [IMMEDIATE ALERT] {t['name']} detected - sending alerts NOW...")
                    try:
                        send_alert_email([t], extra_note="TRUCK DETECTED - Alert sent the INSTANT truck was found! This is an IMMEDIATE alert sent before any auto-buy processing.")
                        send_telegram_alert([t], extra_note="IMMEDIATE ALERT - Truck detected! Alert sent FIRST before auto-buy.")
                        log_cron(f"IMMEDIATE ALERT SENT for {t['name']} (before any other processing)")
                    except Exception as e:
                        log_cron(f"IMMEDIATE ALERT ERROR: {e}")
                        print(f"  [ALERT ERROR] {e}")

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

        alert_sent = False
        action_taken = ""

        if newly_available:
            print("NEW trucks available: " + str([t["name"] for t in newly_available]))
            log_cron(f"New trucks available: {[t['name'] for t in newly_available]}")

            for truck in newly_available:
                truck_name = truck["name"]
                truck_url = truck.get("url", GOOD360_URL)

                # 1. ALERT-FIRST: Send availability alert BEFORE auto-buy
                print(f"  [ALERT-FIRST] Sending availability alert for {truck_name}...")
                send_alert_email([truck],
                    extra_note="Truck is available. Auto-buy will be attempted if applicable.")
                send_telegram_alert([truck],
                    extra_note="Truck available. Auto-buy will be attempted if applicable.")
                log_cron(f"Alert sent for {truck_name}")

                # 2. MULTI-ORG AUTO-BUY: Find which org should buy this truck
                truck_lower = truck_name.lower()
                is_autobuy_target = "new unsorted" in truck_lower or "variety" in truck_lower or "houseware" in truck_lower

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

                    if status == "SUCCESS":
                        send_purchase_confirmation(
                            truck_name,
                            checkout_details.get("order_total") or 0.0,
                            org_name=org_name,
                            details=checkout_details,
                            org_config=matching_org,
                        )
                        action_taken = f"AUTO-BUY SUCCESS ({org_name})"
                        log_cron(f"Auto-buy success for {truck_name} ({org_name})")
                    elif status == "MISSED":
                        action_taken = f"AUTO-BUY MISSED ({org_name}) - ALERT SENT"
                        log_cron(f"Auto-buy missed for {truck_name} ({org_name}): {msg}")
                    elif status == "MANUAL":
                        send_checkout_failure_alert(
                            truck_name,
                            truck_url,
                            status,
                            msg,
                            org_name=org_name,
                            details=checkout_details,
                            org_config=matching_org,
                        )
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
                        )
                        action_taken = f"AUTO-BUY FAILED ({org_name}, {status}) - ALERT SENT"
                        log_cron(f"Auto-buy failed for {truck_name} ({org_name}): {status} - {msg}")
                else:
                    print("  [ALERT ONLY] " + truck_name)
                    action_taken = "ALERT SENT"
                    log_cron(f"Alert-only for {truck_name} (not auto-buy target)")

                if truck_name not in state["alerted"]:
                    state["alerted"].append(truck_name)
                alert_sent = True

        save_state(state)
        append_run_log(now, trucks, alert_sent, action_taken)

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
    result = main()
    print("Result: " + result)
