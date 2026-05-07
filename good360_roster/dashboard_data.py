#!/usr/bin/env python3
"""E-Comsetter Dashboard Data Generator
Reads from roster.db, cron log, and run log to produce dashboard_data.json"""
import json
import os
import re
import sqlite3
from datetime import datetime

# Paths (override via WORKDIR / DASHBOARD_OUTPUT env vars)
_WORKDIR = os.environ.get('WORKDIR', '/a0/usr/workdir')
DB_PATH = f'{_WORKDIR}/good360_roster/db/roster.db'
CRON_LOG = f'{_WORKDIR}/good360_cron.log'
RUN_LOG = f'{_WORKDIR}/good360_run_log.json'
OUTPUT = os.environ.get('DASHBOARD_OUTPUT', '/a0/webui/dashboard_data.json')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def safe_query(conn, sql, params=()):
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []

def get_stats(conn):
    active = conn.execute("SELECT COUNT(*) FROM nonprofits WHERE status='active'").fetchone()[0]
    total_orgs = conn.execute("SELECT COUNT(*) FROM nonprofits").fetchone()[0]
    trucks = conn.execute("SELECT COUNT(*) FROM truck_events").fetchone()[0]
    purchases = conn.execute("SELECT COUNT(*) FROM purchase_attempts WHERE status='success'").fetchone()[0]
    total_attempts = conn.execute("SELECT COUNT(*) FROM purchase_attempts").fetchone()[0]
    errors_24h = conn.execute("SELECT COUNT(*) FROM system_events WHERE severity='error' AND created_at >= datetime('now', '-1 day')").fetchone()[0]
    warnings_24h = conn.execute("SELECT COUNT(*) FROM system_events WHERE severity='warning' AND created_at >= datetime('now', '-1 day')").fetchone()[0]
    health = max(0, 100 - (errors_24h * 15) - (warnings_24h * 5))
    return {
        'active_orgs': active,
        'total_orgs': total_orgs,
        'trucks_detected': trucks,
        'successful_purchases': purchases,
        'total_attempts': total_attempts,
        'health_score': health,
        'errors_24h': errors_24h,
        'warnings_24h': warnings_24h
    }

def get_nonprofits(conn):
    return safe_query(conn, """
        SELECT n.id, n.org_name, n.status, n.queue_position, n.last_purchase_date,
               n.cooldown_until, n.auto_buy_global, n.total_trucks_found,
               n.contact_name, n.contact_email, n.subscription_active,
               n.master_card_fallback, n.joined_date
        FROM nonprofits n ORDER BY n.queue_position ASC
    """)

def get_truck_events(conn):
    rows = safe_query(conn, """
        SELECT t.id, t.detected_at, t.truck_title, t.truck_url, t.truck_price,
               t.truck_location, t.truck_category, t.status, t.assigned_to_org_id,
               n.org_name as assigned_org
        FROM truck_events t
        LEFT JOIN nonprofits n ON t.assigned_to_org_id = n.id
        ORDER BY t.detected_at DESC LIMIT 20
    """)
    return rows

def get_purchase_attempts(conn):
    return safe_query(conn, """
        SELECT pa.id, pa.started_at, pa.completed_at, pa.status, pa.mode,
               pa.confirmation_number, pa.order_total, pa.error_message,
               pa.attempt_number,
               n.org_name,
               t.truck_title, t.truck_price,
               COALESCE(pm.card_last4, 'N/A') as payment_last4
        FROM purchase_attempts pa
        LEFT JOIN nonprofits n ON pa.nonprofit_id = n.id
        LEFT JOIN truck_events t ON pa.truck_event_id = t.id
        LEFT JOIN nonprofit_payment_methods pm ON pa.payment_method_id = pm.id
        ORDER BY pa.started_at DESC LIMIT 20
    """)

def get_system_config(conn):
    return safe_query(conn, "SELECT key, value, updated_at FROM system_config ORDER BY key")

def get_system_events(conn):
    return safe_query(conn, """
        SELECT se.id, se.event_type, se.severity, se.message, se.created_at,
               n.org_name
        FROM system_events se
        LEFT JOIN nonprofits n ON se.nonprofit_id = n.id
        ORDER BY se.created_at DESC LIMIT 50
    """)

def get_billing(conn):
    return safe_query(conn, """
        SELECT b.id, b.billing_type, b.amount, b.currency, b.billing_date,
               b.due_date, b.paid_date, b.status, b.invoice_number, b.notes,
               n.org_name
        FROM billing_records b
        LEFT JOIN nonprofits n ON b.nonprofit_id = n.id
        ORDER BY b.billing_date DESC LIMIT 20
    """)

def parse_cron_log():
    """Parse the cron log into structured run entries (last 20)"""
    runs = []
    if not os.path.exists(CRON_LOG):
        return runs
    try:
        with open(CRON_LOG) as f:
            content = f.read()
        # Split on timestamp lines
        blocks = re.split(r'(?=^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\])', content, flags=re.MULTILINE)
        for block in blocks:
            block = block.strip()
            if not block:
                continue
            ts_match = re.match(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', block)
            if not ts_match:
                continue
            timestamp = ts_match.group(1)
            trucks_found = 0
            fm = re.search(r'Found (\d+) trucks', block)
            if fm:
                trucks_found = int(fm.group(1))
            # Extract result line
            result = ''
            rm = re.search(r'Result: (.+)', block)
            if rm:
                result = rm.group(1).strip()
            # Auto-buy status
            auto_buy = 'INACTIVE'
            abm = re.search(r'AUTO-BUY: (\w+)', block)
            if abm:
                auto_buy = abm.group(1)
            # Count tracked/skipped/available
            tracked = len(re.findall(r'\[TRACKED\]', block))
            skipped = len(re.findall(r'\[skipped\]', block))
            available = len(re.findall(r'AVAILABLE', block))
            runs.append({
                'timestamp': timestamp,
                'trucks_found': trucks_found,
                'tracked': tracked,
                'skipped': skipped,
                'available': available,
                'auto_buy': auto_buy,
                'result': result,
                'raw': block[:500]
            })
    except Exception as e:
        runs.append({'error': str(e)})
    return runs[-20:]  # Last 20

def parse_run_log():
    """Parse the JSON run log (last 10 entries)"""
    if not os.path.exists(RUN_LOG):
        return []
    try:
        with open(RUN_LOG) as f:
            data = json.load(f)
        entries = data.get('runs', [])
        result = []
        for entry in entries[-10:]:
            trucks_summary = []
            for t in entry.get('trucks', []):
                trucks_summary.append({
                    'name': t.get('name', ''),
                    'available': t.get('available', False),
                    'tracked': t.get('tracked', False)
                })
            result.append({
                'time': entry.get('time', ''),
                'alert_sent': entry.get('alert_sent', False),
                'action': entry.get('action', ''),
                'trucks': trucks_summary
            })
        return result
    except Exception as e:
        return [{'error': str(e)}]

def main():
    conn = get_db()
    try:
        data = {
            'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'stats': get_stats(conn),
            'nonprofits': get_nonprofits(conn),
            'truck_events': get_truck_events(conn),
            'purchase_attempts': get_purchase_attempts(conn),
            'monitor_activity': {
                'cron_runs': parse_cron_log(),
                'run_log': parse_run_log()
            },
            'system_config': get_system_config(conn),
            'system_events': get_system_events(conn),
            'billing': get_billing(conn)
        }
        os.makedirs(os.path.dirname(OUTPUT), exist_ok=True)
        with open(OUTPUT, 'w') as f:
            json.dump(data, f, indent=2, default=str)
        print(f"Dashboard data written to {OUTPUT} at {data['generated_at']}")
    finally:
        conn.close()

if __name__ == '__main__':
    main()
