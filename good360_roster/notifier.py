"""
notifier.py — E-Comsetter Good360 Roster System
Email + Twilio-Ready SMS Notification Service
Built: 2026-03-20
"""

import logging
import os
import smtplib
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from queue import Queue
from threading import Thread
from typing import Any

logger = logging.getLogger("notifier")

# ─── DB Path ──────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "db" / "roster.db"


# ─── Async Notification Queue ─────────────────────────────────────────────────
_notification_queue: Queue = Queue()
_dispatch_worker: Thread | None = None


def start_dispatch_worker():
    """Start background thread for async email/SMS dispatch."""
    global _dispatch_worker
    if _dispatch_worker and _dispatch_worker.is_alive():
        return
    _dispatch_worker = Thread(target=_dispatch_loop, daemon=True, name="NotifierDispatch")
    _dispatch_worker.start()
    logger.info("Notification dispatch worker started")


def _dispatch_loop():
    """Background worker: process notifications from queue."""
    while True:
        job = _notification_queue.get()
        if job is None:
            break
        try:
            _send_notification_sync(**job)
        except Exception as e:
            logger.error(f"Notification dispatch failed: {e}")
        finally:
            _notification_queue.task_done()


def queue_notification(notif_type: str, org_id: int, subject: str,
                       body_html: str, body_text: str = None,
                       sms_body: str = None, attachments: list[str] = None):
    """
    Queue a notification for async delivery.
    - Email via SMTP
    - SMS via Twilio (if configured)
    """
    _notification_queue.put({
        "notif_type": notif_type,
        "org_id": org_id,
        "subject": subject,
        "body_html": body_html,
        "body_text": body_text,
        "sms_body": sms_body,
        "attachments": attachments or [],
    })


# ─── Config ───────────────────────────────────────────────────────────────────
def get_config(key: str, default: str = None) -> str:
    with get_db_connection() as conn:
        row = conn.execute("SELECT value FROM system_config WHERE key = ?",
                          (key,)).fetchone()
        return row[0] if row else default


# ─── DB ──────────────────────────────────────────────────────────────────────
@contextmanager
def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


# ─── Email Config ─────────────────────────────────────────────────────────────
def get_smtp_config() -> dict[str, Any]:
    """Get SMTP settings from environment / .env."""
    return {
        "smtp_host":     os.environ.get("SMTP_HOST", "smtp.gmail.com"),
        "smtp_port":     int(os.environ.get("SMTP_PORT", "587")),
        "smtp_user":     os.environ.get("SMTP_USER", ""),
        "smtp_password": os.environ.get("SMTP_PASSWORD", ""),
        "smtp_tls":      os.environ.get("SMTP_TLS", "true").lower() == "true",
        "from_address":  os.environ.get("NOTIFIER_FROM", "alerts@e-comsetter.com"),
        "from_name":     os.environ.get("NOTIFIER_FROM_NAME", "Good360 Roster System"),
    }


def send_email(to_email: str, subject: str, body_html: str,
               body_text: str = None, attachments: list[str] = None,
               cc: list[str] = None, bcc: list[str] = None,
               smtp_config: dict[str, Any] = None) -> bool:
    """
    Send an email via SMTP.
    Returns True on success, False on failure.
    """
    if smtp_config is None:
        smtp_config = get_smtp_config()

    # Sandbox-mode tag: prepend [SANDBOX] to subject + body so test emails
    # can never be confused with real ones in the operator's inbox.
    try:
        import sys as _sys, os as _os
        _root = _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        if _root not in _sys.path:
            _sys.path.insert(0, _root)
        import sandbox as _sandbox  # noqa: WPS433
        if _sandbox.is_sandbox():
            subject = _sandbox.decorate_alert(subject)
            body_html = "<p style='background:#fee;color:#900;padding:6px 10px;font-family:monospace'>[SANDBOX] test mode — not a live alert</p>" + body_html
            if body_text:
                body_text = _sandbox.decorate_alert(body_text)
    except Exception:
        pass

    if not smtp_config["smtp_user"] or not smtp_config["smtp_password"]:
        logger.warning(f"SMTP not configured — skipping email to {to_email}")
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{smtp_config['from_name']} <{smtp_config['from_address']}>"
    msg["To"] = to_email
    if cc:
        msg["Cc"] = ", ".join(cc)

    # Plain text fallback
    if body_text:
        msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    # Attachments
    for filepath in attachments or []:
        try:
            with open(filepath, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header("Content-Disposition",
                               f"attachment; filename={Path(filepath).name}")
                msg.attach(part)
        except Exception as e:
            logger.warning(f"Failed to attach {filepath}: {e}")

    try:
        with smtplib.SMTP(smtp_config["smtp_host"],
                          smtp_config["smtp_port"]) as server:
            server.ehlo()
            if smtp_config["smtp_tls"]:
                server.starttls()
                server.ehlo()
            server.login(smtp_config["smtp_user"], smtp_config["smtp_password"])
            recipients = [to_email] + (cc or []) + (bcc or [])
            server.sendmail(smtp_config["from_address"], recipients,
                           msg.as_string())
        logger.info(f"Email sent: {to_email} | {subject}")
        return True
    except Exception as e:
        logger.error(f"Email failed to {to_email}: {e}")
        return False


# ─── Twilio SMS ─────────────────────────────────────────────────────────────
def send_sms(to_phone_e164: str, body: str) -> bool:
    """
    Send SMS via Twilio REST API.
    Returns True on success, False on failure.
    Requires: twilio_account_sid, twilio_auth_token, twilio_from_number in system_config.
    """
    account_sid = get_config("twilio_account_sid")
    auth_token  = get_config("twilio_auth_token")
    from_number = get_config("twilio_from_number")

    if not account_sid or not auth_token or not from_number:
        logger.debug(f"Twilio not configured — skipping SMS to {to_phone_e164}")
        return False

    if not to_phone_e164.startswith("+"):
        logger.warning(f"Phone {to_phone_e164} not in E.164 format — skipping SMS")
        return False

    try:
        from twilio.rest import Client
        client = Client(account_sid, auth_token)
        message = client.messages.create(
            body=body[:1600],  # Twilio limit
            from_=from_number,
            to=to_phone_e164
        )
        logger.info(f"SMS sent to {to_phone_e164}: SID={message.sid}")
        return True
    except ImportError:
        logger.warning("twilio library not installed — SMS disabled")
        return False
    except Exception as e:
        logger.error(f"SMS failed to {to_phone_e164}: {e}")
        return False


# ─── Notification Templates ───────────────────────────────────────────────────
BRAND = "E-Comsetter Good360"
SYSTEM_EMAIL = "alerts@e-comsetter.com"


def _html_wrapper(title: str, body_html: str, org_name: str = None) -> str:
    return f"""<!DOCTYPE html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body{{font-family:Arial,sans-serif;background:#f4f4f4;margin:0;padding:20px}}
  .container{{max-width:600px;margin:0 auto;background:#fff;
             border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,.1)}}
  .header{{background:#1a6b3a;padding:20px;color:#fff;text-align:center}}
  .header h1{{margin:0;font-size:20px}}
  .content{{padding:30px}}
  .truck-card{{background:#f8fff8;border:2px solid #1a6b3a;
               border-radius:8px;padding:20px;margin:15px 0}}
  .truck-title{{font-size:18px;font-weight:bold;color:#1a6b3a;margin-bottom:8px}}
  .truck-price{{font-size:24px;font-weight:bold;color:#222}}
  .truck-meta{{color:#666;font-size:13px;margin-top:5px}}
  .cta-button{{display:inline-block;background:#1a6b3a;color:#fff;
               padding:12px 30px;border-radius:6px;text-decoration:none;
               font-weight:bold;margin:10px 5px}}
  .cta-secondary{{background:#666}}
  .footer{{background:#eee;padding:15px;text-align:center;
           font-size:12px;color:#888}}
  .alert{{background:#fff3cd;border:1px solid #ffc107;
          border-radius:6px;padding:15px;margin:15px 0}}
  .alert-danger{{background:#f8d7da;border-color:#f5c2c7;color:#842029}}
</style>
</head><body>
<div class="container">
  <div class="header"><h1>{BRAND}</h1></div>
  <div class="content">{body_html}</div>
  <div class="footer">
    {org_name or 'Good360 Roster System'} &bull;
    Auto-generated {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}<br>
    Do not reply to this email.
  </div>
</div>
</body></html>"""


# ─── Notification Types ──────────────────────────────────────────────────────

def notify_truck_alert(org_id: int, truck_event_id: int,
                       truck_title: str, truck_price: float,
                       truck_url: str, truck_location: str,
                       truck_category: str, alert_mode: str = "auto_buy",
                       expiry_minutes: int = 30):
    """
    Alert an org that a truck matching their preferences is available.
    Sent when auto_buy is OFF (alert-only mode).
    """
    with get_db_connection() as conn:
        org = conn.execute("SELECT * FROM nonprofits WHERE id = ?", (org_id,)).fetchone()

    if not org:
        logger.error(f"Org {org_id} not found for truck alert")
        return

    alert_email = org["alert_email"] or org["contact_email"]
    expires_at = datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    if alert_mode == "alert_only":
        expires_at_dt = datetime.utcnow() + timedelta(minutes=expiry_minutes)
        expires_at = expires_at_dt.strftime("%Y-%m-%d %H:%M")

    subject = f"🚚 Truck Available: {truck_title} — ${truck_price:,.0f}"

    alert_block = ""
    if alert_mode == "alert_only":
        alert_block = (
            f'<div class="alert">\n'
            f'  ⏰ <strong>Manual Action Required</strong> — Auto-buy is OFF for your account.<br>\n'
            f'  Please log into Good360 and purchase manually before <strong>{expires_at}</strong>.<br>\n'
            f'  <em>If you miss this window, no cooldown will be applied to your queue position.</em>\n'
            f'</div>'
        )

    body_html = f"""
<h2>Hello {org['contact_name']},</h2>
<p>A truck matching <strong>{org['org_name']}</strong>'s preferences is available:</p>

<div class="truck-card">
  <div class="truck-title">{truck_title}</div>
  <div class="truck-price">${truck_price:,.2f}</div>
  <div class="truck-meta">
    📍 {truck_location or 'Location TBD'} &nbsp;|&nbsp;
    📦 Category: {truck_category or 'General'}
  </div>
  <div class="truck-meta" style="margin-top:10px">
    🔗 {truck_url}
  </div>
</div>

{alert_block}

<p><strong>What's next?</strong></p>
<ul>
  <li>Log into your Good360 account at good360.org</li>
  <li>Navigate to the truck and complete checkout</li>
  <li>After purchase, we'll notify you with confirmation</li>
</ul>

<p style="margin-top:20px">Need help? Reply to this email or contact {SYSTEM_EMAIL}</p>
"""

    sms_body = (f"🚚 {org['org_name']}: Truck available! "
                f"{truck_title} — ${truck_price:,.0f} | {truck_url[:50]}... | "
                f"Manual purchase required before {expires_at}")

    queue_notification(
        notif_type="truck_alert",
        org_id=org_id,
        subject=subject,
        body_html=_html_wrapper(subject, body_html, org["org_name"]),
        body_text=f"Truck alert for {org['org_name']}: {truck_title} at ${truck_price:,.2f}. {truck_url}",
        sms_body=sms_body if org["sms_alerts_enabled"] else None,
    )


def notify_purchase_success(org_id: int, truck_event_id: int,
                             confirmation_number: str, order_total: float,
                             truck_title: str):
    """
    Confirm successful purchase to org.
    """
    with get_db_connection() as conn:
        org = conn.execute("SELECT * FROM nonprofits WHERE id = ?", (org_id,)).fetchone()

    if not org:
        return

    alert_email = org["alert_email"] or org["contact_email"]
    subject = f"✅ Purchase Confirmed: {truck_title} — ${order_total:,.2f}"

    body_html = f"""
<h2>🎉 Congratulations, {org['contact_name']}!</h2>
<p>Your purchase for <strong>{org['org_name']}</strong> has been confirmed.</p>

<div class="truck-card">
  <div class="truck-title">{truck_title}</div>
  <div class="truck-price">${order_total:,.2f}</div>
  <div style="margin-top:10px">
    <strong>Confirmation #:</strong> {confirmation_number}<br>
    <strong>Date:</strong> {datetime.utcnow().strftime('%Y-%m-%d')}
  </div>
</div>

<div class="alert">
  ⏰ <strong>7-Day Cooldown Started</strong><br>
  Your organization is now in a 7-day cooldown period before your next purchase.<br>
  You'll be notified when you're back in the rotation.
</div>

<p>A finding fee of <strong>$500</strong> will be billed to your account.</p>
"""

    queue_notification(
        notif_type="purchase_success",
        org_id=org_id,
        subject=subject,
        body_html=_html_wrapper(subject, body_html, org["org_name"]),
        body_text=f"Purchase confirmed: {truck_title} for {org['org_name']}. "
                  f"Confirmation: {confirmation_number}. Total: ${order_total:,.2f}.",
    )


def notify_master_card_used(org_id: int, purchase_attempt_id: int,
                             truck_price: float, penalty_amount: float,
                             invoice_number: str, admin_email: str = None):
    """
    Alert org + admin that admin master card was used as fallback.
    High-severity notification.
    """
    with get_db_connection() as conn:
        org = conn.execute("SELECT * FROM nonprofits WHERE id = ?", (org_id,)).fetchone()

    if not org:
        return

    total_owed = truck_price + penalty_amount

    # Alert to org
    alert_email = org["alert_email"] or org["contact_email"]
    subject = f"⚠️ Admin Card Used — Invoice {invoice_number}"

    body_html = f"""
<h2 style="color:#842029">⚠️ Admin Card Fallback Activated</h2>
<p>Hello {org['contact_name']},</p>
<p>All 3 of your organization's payment methods were declined for a truck purchase.</p>

<div class="alert alert-danger">
  <strong>Your E-Comsetter admin card was used as a fallback.</strong>
</div>

<h3>Invoice Summary</h3>
<table style="width:100%;border-collapse:collapse">
  <tr><td style="padding:8px;border:1px solid #ddd">Truck Price</td>
      <td style="padding:8px;border:1px solid #ddd;text-align:right">
          <strong>${truck_price:,.2f}</strong></td></tr>
  <tr><td style="padding:8px;border:1px solid #ddd">Penalty (3× finding fee)</td>
      <td style="padding:8px;border:1px solid #ddd;text-align:right">
          <strong>${penalty_amount:,.2f}</strong></td></tr>
  <tr style="background:#f8d7da">
      <td style="padding:8px;border:1px solid #ddd;font-weight:bold">Total Owed</td>
      <td style="padding:8px;border:1px solid #ddd;text-align:right;font-weight:bold">
          ${total_owed:,.2f}</td></tr>
</table>

<p>Invoice Number: <strong>{invoice_number}</strong></p>
<p>Please update your payment methods and resolve this invoice within 30 days.</p>
"""

    queue_notification(
        notif_type="master_card_fallback",
        org_id=org_id,
        subject=subject,
        body_html=_html_wrapper(subject, body_html, org["org_name"]),
        body_text=f"Admin card fallback: {org['org_name']} owes ${total_owed:,.2f}. "
                  f"Invoice: {invoice_number}.",
    )


def notify_subscription_due(org_id: int, amount: float, due_date: str):
    """Send subscription renewal reminder."""
    with get_db_connection() as conn:
        org = conn.execute("SELECT * FROM nonprofits WHERE id = ?", (org_id,)).fetchone()

    alert_email = org["alert_email"] or org["contact_email"]
    subject = f"💳 Subscription Due: ${amount:,.0f}/month — {org['org_name']}"

    body_html = f"""
<h2>Subscription Reminder</h2>
<p>Hello {org['contact_name']},</p>
<p>Your <strong>{org['org_name']}</strong> Good360 platform subscription is due.</p>

<div class="alert">
  <strong>Amount Due:</strong> ${amount:,.2f}/month<br>
  <strong>Due Date:</strong> {due_date}
</div>

<p>Log into your admin portal to update billing and manage your account.</p>
"""

    queue_notification(
        notif_type="subscription_due",
        org_id=org_id,
        subject=subject,
        body_html=_html_wrapper(subject, body_html, org["org_name"]),
        body_text=f"Subscription due: ${amount:,.2f} for {org['org_name']} by {due_date}.",
    )


def notify_admin_alert(subject: str, message: str, severity: str = "error",
                       org_id: int = None):
    """
    Send admin-level alert (master card usage, critical errors, etc.).
    Admin email from NOTIFIER_ADMIN_EMAIL env var.
    """
    admin_email = os.environ.get("NOTIFIER_ADMIN_EMAIL", "admin@e-comsetter.com")
    severity_colors = {"info": "#17a2b8", "warning": "#ffc107",
                       "error": "#dc3545", "critical": "#6f42c1"}
    color = severity_colors.get(severity, "#666")

    org_block = f"<p><strong>Org ID:</strong> {org_id}</p>" if org_id else ""
    body_html = f"""
<h2 style="color:{color}">[{severity.upper()}] {subject}</h2>
<p><strong>Time:</strong> {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</p>
<p><strong>Message:</strong></p>
<pre style="background:#f8f8f8;padding:15px;border-radius:6px">{message}</pre>
{org_block}
<p>Auto-generated by Good360 Roster System.</p>
"""

    queue_notification(
        notif_type="admin_alert",
        org_id=org_id or 0,
        subject=f"[Good360 Admin] {subject}",
        body_html=body_html,
        body_text=f"[{severity.upper()}] {subject} | {message}",
    )


# ─── Sync Dispatch ────────────────────────────────────────────────────────────
def _send_notification_sync(notif_type: str, org_id: int,
                            subject: str, body_html: str,
                            body_text: str, sms_body: str,
                            attachments: list[str]):
    """Synchronously send one notification (email + SMS if applicable)."""
    with get_db_connection() as conn:
        org = conn.execute("SELECT * FROM nonprofits WHERE id = ?",
                           (org_id,)).fetchone()
    if not org:
        logger.error(f"Org {org_id} not found — dropping notification")
        return

    alert_email = org["alert_email"] or org["contact_email"]

    # Email
    email_ok = send_email(
        to_email=alert_email,
        subject=subject,
        body_html=body_html,
        body_text=body_text,
        attachments=attachments,
    )

    # SMS
    sms_ok = False
    if sms_body and org["phone_number"]:
        sms_ok = send_sms(org["phone_number"], sms_body)

    # Log result
    with get_db_connection() as conn:
        conn.execute(
            """INSERT INTO system_events
               (event_type, severity, nonprofit_id, message, metadata_json)
               VALUES (?, 'info', ?, ?, ?)""",
            (f"notification_{notif_type}", org_id,
             f"Email={'OK' if email_ok else 'FAIL'} | SMS={'OK' if sms_ok else 'FAIL'} | {subject}",
             None)
        )
        conn.commit()


# ─── Helpers ─────────────────────────────────────────────────────────────────
from datetime import timedelta


def is_twilio_configured() -> bool:
    sid = get_config("twilio_account_sid")
    token = get_config("twilio_auth_token")
    num = get_config("twilio_from_number")
    return bool(sid and token and num)


def get_notification_history(org_id: int, limit: int = 20) -> list[dict]:
    """Get recent notification events for an org."""
    with get_db_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM system_events
               WHERE nonprofit_id = ? AND event_type LIKE 'notification_%'
               ORDER BY created_at DESC LIMIT ?""",
            (org_id, limit)
        ).fetchall()
    return [dict(row) for row in rows]


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(prog="notifier.py",
                                     description="Notifier service")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("start-worker", help="Start background dispatch worker")
    sub.add_parser("test-email", help="Send test email")
    sub.add_parser("test-sms", help="Send test SMS")
    sub.add_parser("queue-status", help="Show queue size")

    test_email = sub.add_parser("send-test-alert", help="Send test truck alert to org")
    test_email.add_argument("org_id", type=int)

    args = parser.parse_args()

    if args.cmd == "start-worker":
        start_dispatch_worker()
        print("Dispatch worker running. Press Ctrl+C to stop.")
        import time; time.sleep(86400)
    elif args.cmd == "test-email":
        ok = send_email("test@example.com", "Test Subject",
                       "<h1>Test</h1><p>This is a test.</p>",
                       "Test body")
        print(f"Email sent: {ok}")
    elif args.cmd == "test-sms":
        ok = send_sms("+14045551234", "Test SMS from Good360 Roster System")
        print(f"SMS sent: {ok}")
    elif args.cmd == "queue-status":
        print(f"Queue size: {_notification_queue.qsize()}")
    elif args.cmd == "send-test-alert":
        notify_truck_alert(
            org_id=args.org_id,
            truck_event_id=999,
            truck_title="2024 Amazon Truck — Mixed Electronics",
            truck_price=4250.0,
            truck_url="https://good360.org/trucks/12345",
            truck_location="Atlanta, GA",
            truck_category="amazon_new_unsorted",
            alert_mode="alert_only",
        )
        print(f"Test alert queued for org {args.org_id}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
