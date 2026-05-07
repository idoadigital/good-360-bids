#!/usr/bin/env python3
"""E-Comsetter Good360 Roster System — Web Dashboard"""
import json
import os
import re
import sqlite3
from datetime import datetime, timedelta, timezone

from flask import Flask, jsonify, render_template

app = Flask(__name__)

_WORKDIR = os.environ.get("WORKDIR", "/a0/usr/workdir")
DB_PATH = os.path.join(os.path.dirname(__file__), "db", "roster.db")
CRON_LOG = f"{_WORKDIR}/good360_cron.log"
RUN_LOG = f"{_WORKDIR}/good360_run_log.json"
ET = timezone(timedelta(hours=-4))  # EDT


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def safe_query(query, params=(), limit=None):
    """Execute query, return list of dicts. Handles missing tables."""
    try:
        conn = get_db()
        cur = conn.cursor()
        if limit:
            query += f" LIMIT {int(limit)}"
        cur.execute(query, params)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
        conn.close()
        return rows
    except Exception:
        return []


def parse_cron_log(max_runs=20):
    """Parse cron log into structured run blocks."""
    if not os.path.exists(CRON_LOG):
        return []
    try:
        with open(CRON_LOG) as f:
            content = f.read()
    except Exception:
        return []

    # Split into run blocks by the timestamp pattern
    blocks = re.split(r"(?=^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\])", content, flags=re.MULTILINE)
    blocks = [b.strip() for b in blocks if b.strip()]

    runs = []
    for block in blocks[-max_runs:]:
        ts_match = re.match(r"\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]", block)
        if not ts_match:
            continue

        timestamp = ts_match.group(1)

        # Count trucks found
        trucks_found_match = re.search(r"Found (\d+) trucks?", block)
        trucks_found = int(trucks_found_match.group(1)) if trucks_found_match else 0

        # Count available
        available = len(re.findall(r"-> AVAILABLE", block, re.IGNORECASE))

        # Get result/action
        result_match = re.search(r"Result: (.+)", block)
        action = result_match.group(1).strip() if result_match else ""
        if not action:
            if "AUTO-BUY" in block and "TRIGGERED" in block:
                action = "Auto-buy triggered"
            elif "ALERT SENT" in block.upper():
                action = "Alert sent"
            elif "No new available" in block:
                action = "No new available trucks"
            else:
                # Get last non-empty line
                lines = [l.strip() for l in block.split("\n") if l.strip()]
                action = lines[-1] if lines else "Check completed"

        auto_buy_active = "AUTO-BUY: ACTIVE" in block or "AUTO-BUY ACTIVE" in block

        runs.append({
            "timestamp": timestamp,
            "trucks_found": trucks_found,
            "available": available,
            "action": action,
            "auto_buy_active": auto_buy_active
        })

    return runs


def parse_run_log(max_entries=10):
    """Parse good360_run_log.json."""
    if not os.path.exists(RUN_LOG):
        return []
    try:
        with open(RUN_LOG) as f:
            data = json.load(f)
        entries = data.get("runs", [])[-max_entries:]
        result = []
        for entry in entries:
            trucks = entry.get("trucks", [])
            total = len(trucks)
            available = sum(1 for t in trucks if t.get("available"))
            tracked = sum(1 for t in trucks if t.get("tracked"))
            truck_names = [t.get("name", "Unknown") for t in trucks if t.get("available")]
            result.append({
                "time": entry.get("time", ""),
                "alert_sent": entry.get("alert_sent", False),
                "action": entry.get("action", ""),
                "total_trucks": total,
                "available": available,
                "tracked": tracked,
                "available_trucks": truck_names
            })
        return result
    except Exception:
        return []


# ─── Routes ───────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("dashboard.html")


@app.route("/api/time")
def api_time():
    now_et = datetime.now(ET)
    return jsonify({"time": now_et.strftime("%Y-%m-%d %I:%M:%S %p ET")})


@app.route("/api/stats")
def api_stats():
    try:
        conn = get_db()
        cur = conn.cursor()

        cur.execute("SELECT COUNT(*) FROM nonprofits WHERE status = \"active\"")
        active_orgs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM nonprofits")
        total_orgs = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM truck_events")
        total_trucks = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM purchase_attempts WHERE status IN (\"success\", \"success_mastercard\")")
        successful_purchases = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM purchase_attempts WHERE status = \"failed_payment\" OR status = \"failed_login\" OR status = \"failed_checkout\"")
        failed_purchases = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM system_events WHERE severity = \"error\" OR severity = \"critical\"")
        error_events = cur.fetchone()[0]

        cur.execute("SELECT COUNT(*) FROM system_events")
        total_events = cur.fetchone()[0]

        conn.close()

        # Health score: 100 minus penalties
        health = 100
        if error_events > 0:
            health -= min(error_events * 10, 40)
        if failed_purchases > 0:
            health -= min(failed_purchases * 15, 30)
        health = max(0, health)

        # Determine status color
        if health >= 80:
            status = "healthy"
        elif health >= 50:
            status = "warning"
        else:
            status = "critical"

        return jsonify({
            "active_orgs": active_orgs,
            "total_orgs": total_orgs,
            "total_trucks": total_trucks,
            "successful_purchases": successful_purchases,
            "failed_purchases": failed_purchases,
            "health_score": health,
            "system_status": status,
            "error_events": error_events,
            "total_events": total_events
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/nonprofits")
def api_nonprofits():
    rows = safe_query("""
        SELECT id, org_name, status, queue_position, last_purchase_date,
               cooldown_until, auto_buy_global, total_trucks_found,
               contact_name, contact_email, subscription_active,
               master_card_fallback, sms_alerts_enabled
        FROM nonprofits
        ORDER BY queue_position ASC, org_name ASC
    """)
    return jsonify(rows)


@app.route("/api/truck-events")
def api_truck_events():
    rows = safe_query("""
        SELECT te.id, te.detected_at, te.truck_title, te.truck_price,
               te.truck_location, te.truck_category, te.status,
               te.assigned_to_org_id, n.org_name as assigned_org_name
        FROM truck_events te
        LEFT JOIN nonprofits n ON te.assigned_to_org_id = n.id
        ORDER BY te.detected_at DESC
    """, limit=20)
    return jsonify(rows)


@app.route("/api/purchase-attempts")
def api_purchase_attempts():
    rows = safe_query("""
        SELECT pa.id, pa.started_at, pa.completed_at, pa.status, pa.mode,
               pa.confirmation_number, pa.order_total, pa.error_message,
               pa.attempt_number,
               n.org_name, te.truck_title,
               pm.card_last4 as payment_card
        FROM purchase_attempts pa
        LEFT JOIN nonprofits n ON pa.nonprofit_id = n.id
        LEFT JOIN truck_events te ON pa.truck_event_id = te.id
        LEFT JOIN nonprofit_payment_methods pm ON pa.payment_method_id = pm.id
        ORDER BY pa.started_at DESC
    """, limit=30)
    return jsonify(rows)


@app.route("/api/monitor-activity")
def api_monitor_activity():
    cron_runs = parse_cron_log(20)
    run_log = parse_run_log(10)

    # Get last cron run time
    last_run = cron_runs[-1]["timestamp"] if cron_runs else None

    return jsonify({
        "cron_runs": list(reversed(cron_runs)),  # newest first
        "run_log": list(reversed(run_log)),  # newest first
        "last_run": last_run
    })


@app.route("/api/system-config")
def api_system_config():
    rows = safe_query("SELECT key, value, updated_at FROM system_config ORDER BY key")
    return jsonify(rows)


@app.route("/api/system-events")
def api_system_events():
    rows = safe_query("""
        SELECT se.id, se.event_type, se.severity, se.message,
               se.metadata_json, se.created_at,
               n.org_name
        FROM system_events se
        LEFT JOIN nonprofits n ON se.nonprofit_id = n.id
        ORDER BY se.created_at DESC
    """, limit=50)
    return jsonify(rows)


@app.route("/api/billing")
def api_billing():
    rows = safe_query("""
        SELECT br.id, br.billing_type, br.amount, br.status,
               br.billing_date, br.paid_date, br.invoice_number,
               n.org_name
        FROM billing_records br
        LEFT JOIN nonprofits n ON br.nonprofit_id = n.id
        ORDER BY br.billing_date DESC
    """, limit=20)
    return jsonify(rows)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
