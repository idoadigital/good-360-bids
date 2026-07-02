
import json
import os
import smtplib
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import requests

# ============================================================
# CONFIGURATION
# ============================================================
SMTP_USER = os.environ.get("ALERT_EMAIL_FROM", "")
SMTP_PASS = os.environ.get("SMTP_PASSWORD", "")
ALERT_TO = [e.strip() for e in os.environ.get("ALERT_EMAIL_TO", "").split(",") if e.strip()]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_GROUP_HOPE4HUMANITY", "")
LOG_FILE = f"{os.environ.get('WORKDIR', '/a0/usr/workdir')}/good360_run_log.json"

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox  # noqa: E402  (sandbox-mode URL routing)
import feature_flags  # noqa: E402
GOOD360_URL = sandbox.good360_browse_url()
REPORT_HOURS = 6


TELEGRAM_CHAT_IDS = ["-1003866351492", "-5089630277"]  # Hope4Humanity + Reviving Homes

def send_telegram(message):
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("telegram"))
        return
    # Truncate if too long (Telegram limit: 4096 chars)
    if len(message) > 4000:
        # Keep header, truncate body, add note
        lines = message.split("\n")
        truncated = []
        char_count = 0
        for line in lines:
            if char_count + len(line) > 3800:
                truncated.append("\n... (truncated - see email for full report)")
                break
            truncated.append(line)
            char_count += len(line)
        message = "\n".join(truncated)

    any_delivered = False
    last_err = None
    for chat_id in TELEGRAM_CHAT_IDS:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
            payload = {"chat_id": chat_id, "text": message, "parse_mode": "HTML", "disable_web_page_preview": True}
            result = requests.post(url, json=payload, timeout=10).json()
            if result.get("ok"):
                any_delivered = True
                print(f"Telegram sent to {chat_id}!")
            else:
                last_err = str(result)
                print(f"Telegram error for {chat_id}: {result}")
        except Exception as e:
            last_err = str(e)
            print(f"Telegram failed for {chat_id}: {e}")
    try:
        from notifications_log import record_telegram
        record_telegram(source='report', message=message, delivered=any_delivered, error=last_err, channel='all-orgs')
    except Exception:
        pass


def main():
    now = datetime.now()
    cutoff = now - timedelta(hours=REPORT_HOURS)
    daily_mode = os.getenv('GOOD360_DAILY', '0') == '1'
    if daily_mode:
        cutoff = datetime(now.year, now.month, now.day, 0, 0, 0)
    now_str = now.strftime(' %Y-%m-%d %H:%M:%S')

    # Load log
    if not os.path.exists(LOG_FILE):
        print("No log file found yet.")
        return

    with open(LOG_FILE) as f:
        log = json.load(f)

    # Filter runs from last 6 hours
    recent_runs = []
    for run in log.get("runs", []):
        try:
            run_time = datetime.strptime(run["time"].strip(), '%Y-%m-%d %H:%M:%S')
            if run_time >= cutoff:
                recent_runs.append(run)
        except:
            pass

    total_runs = len(recent_runs)
    alert_runs = [r for r in recent_runs if r.get("alert_sent")]
    no_truck_runs = [r for r in recent_runs if not r.get("alert_sent")]

    print(f"Total runs in last {REPORT_HOURS}h: {total_runs}")
    print(f"Alerts sent: {len(alert_runs)}")

    # Build run rows for email
    run_rows = ""
    for run in recent_runs:
        alert_badge = "<span style='background:#e74c3c;color:white;padding:2px 8px;border-radius:10px;font-size:11px;'>🚨 ALERT SENT</span>" if run.get("alert_sent") else "<span style='background:#95a5a6;color:white;padding:2px 8px;border-radius:10px;font-size:11px;'>No trucks</span>"
        run_rows += f"<tr><td style='padding:8px;border:1px solid #ddd;font-size:13px;'>{run['time']}</td><td style='padding:8px;border:1px solid #ddd;text-align:center;'>{alert_badge}</td></tr>"

    if not run_rows:
        run_rows = "<tr><td colspan='2' style='padding:12px;text-align:center;color:#888;'>No runs recorded yet in this period</td></tr>"

    # Build email HTML
    html = f"""
    <html><body style="font-family:Arial,sans-serif;max-width:700px;margin:auto;padding:20px;">
    <div style="background:#2c3e50;padding:20px;border-radius:8px 8px 0 0;text-align:center;">
        <h2 style="color:white;margin:0;">📊 E-Comsetter Good360 Monitor</h2>
        <p style="color:#bdc3c7;margin:5px 0 0 0;">6-Hour Activity Report</p>
    </div>
    <div style="border:1px solid #ddd;border-top:none;padding:20px;border-radius:0 0 8px 8px;">

        <p style="color:#7f8c8d;">Report generated: <strong>{now_str}</strong></p>
        <p>Summary of monitoring activity for the <strong>last {REPORT_HOURS} hours</strong> ({cutoff.strftime('%Y-%m-%d %H:%M')} → {now.strftime('%H:%M')}):</p>

        <div style="display:flex;gap:15px;margin:20px 0;">
            <div style="flex:1;background:#eaf6ff;border-left:4px solid #3498db;padding:15px;border-radius:4px;text-align:center;">
                <div style="font-size:32px;font-weight:bold;color:#2980b9;">{total_runs}</div>
                <div style="color:#7f8c8d;font-size:13px;">Total Checks Run</div>
            </div>
            <div style="flex:1;background:#eafaf1;border-left:4px solid #27ae60;padding:15px;border-radius:4px;text-align:center;">
                <div style="font-size:32px;font-weight:bold;color:#27ae60;">{len(alert_runs)}</div>
                <div style="color:#7f8c8d;font-size:13px;">Alerts Triggered</div>
            </div>
            <div style="flex:1;background:#fef9e7;border-left:4px solid #f39c12;padding:15px;border-radius:4px;text-align:center;">
                <div style="font-size:32px;font-weight:bold;color:#f39c12;">{len(no_truck_runs)}</div>
                <div style="color:#7f8c8d;font-size:13px;">No Trucks Found</div>
            </div>
        </div>

        <h3 style="color:#2c3e50;border-bottom:2px solid #ecf0f1;padding-bottom:8px;">🕐 Run History</h3>
        <table style="border-collapse:collapse;width:100%;">
            <tr style="background:#2c3e50;color:white;">
                <th style="padding:10px;border:1px solid #ddd;text-align:left;">Time</th>
                <th style="padding:10px;border:1px solid #ddd;text-align:center;">Result</th>
            </tr>
            {run_rows}
        </table>

        <br>
        <div style="background:#f0f9ff;border-left:4px solid #3498db;padding:15px;border-radius:4px;">
            <p style="margin:4px 0;">⏱️ <strong>Check frequency:</strong> Every 15 minutes</p>
            <p style="margin:4px 0;">🔍 <strong>Tracking:</strong> Unsorted, Variety, Assorted (non-softline)</p>
            <p style="margin:4px 0;">📋 <strong>Next 6h report:</strong> {(now + timedelta(hours=6)).strftime('%Y-%m-%d %H:%M')}</p>
        </div>

        <br>
        <p style="text-align:center;">
            <a href="{GOOD360_URL}" style="background:#27ae60;color:white;padding:12px 28px;text-decoration:none;border-radius:5px;font-size:15px;">👉 View Good360 Amazon Truckloads</a>
        </p>
        <br>
        <p style="color:#bdc3c7;font-size:12px;text-align:center;">— E-Comsetter Good360 Monitor | Automated 6-hour report</p>
    </div>
    </body></html>
    """

    # Send Email
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("report email"))
        return
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"📊 Good360 Monitor — 6-Hour Report | {now_str}"
    msg["From"] = SMTP_USER
    msg["To"] = ", ".join(ALERT_TO)
    msg.attach(MIMEText(html, "html"))
    # 587 + STARTTLS — port 465 (SMTPS) is blocked outbound on this host.
    with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
        server.starttls()
        server.login(SMTP_USER, SMTP_PASS)
        server.sendmail(SMTP_USER, ALERT_TO, msg.as_string())
    print(f"6-hour report email sent to: {ALERT_TO}")

    # Build Telegram summary
    # Build Telegram summary
    if daily_mode:
        total_truck_hits = sum(len([t for t in r.get('trucks', []) if t.get('available')]) for r in recent_runs)
        total_scans = len(recent_runs)
        tg_msg = (
            "[Daily Summary] <b>E-Comsetter Good360</b>\n"
            + cutoff.strftime('%H:%M') + " -> " + now.strftime('%H:%M') + " (" + now.strftime('%Y-%m-%d') + ")\n"
            + "\n<b>Summary:</b>\n"
            + "Total scans today: " + str(total_scans) + "\n"
            + "Trucks detected: " + str(total_truck_hits) + "\n"
            + "\nOperations complete for the day.\n"
            + "-- E-Comsetter Good360 Monitor"
        )
    else:
        run_lines = "\n".join([
            f"{'[ALERT]' if r.get('alert_sent') else '[OK]'} {r['time']} - {'ALERT SENT' if r.get('alert_sent') else 'No trucks found'}"
            for r in recent_runs
        ]) or "No runs recorded yet."
        tg_msg = f"""[CHART] <b>E-Comsetter Good360 -- 6-Hour Report</b>
[TIME] {cutoff.strftime('%H:%M')} -> {now.strftime('%H:%M')} ({now.strftime('%Y-%m-%d')})

<b>Summary:</b>
[LOOP] Total checks: {total_runs}
[ALERT] Alerts sent: {len(alert_runs)}
[OK] No trucks: {len(no_truck_runs)}

<b>Run log:</b>
{run_lines}

[TIMER] Checks every 15 mins | Next report in 6h
-- E-Comsetter Good360 Monitor"""

    send_telegram(tg_msg)

if __name__ == "__main__":
    main()
