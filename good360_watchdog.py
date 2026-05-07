#!/usr/bin/env python3
"""Good360 Watchdog - FIXED VERSION with proper ET timezone handling"""
import json
import os
import subprocess
from datetime import datetime

import pytz
import requests

# Config
STATE_FILE = 'good360_watchdog_state.json'
CRON_LOG = 'good360_cron.log'
ALERT_LOG = 'good360_watchdog_alerts.log'
MAX_STALE_MINUTES = 5
ET = pytz.timezone('America/New_York')

# Telegram config (values from .env — see .env.example)
BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = os.environ.get('TELEGRAM_GROUP_HOPE4HUMANITY', '')

def log_alert(msg):
    with open(ALERT_LOG, 'a') as f:
        f.write(f"[{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

def send_telegram_alert(message):
    try:
        url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
        data = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
        r = requests.post(url, json=data, timeout=10)
        log_alert(f"Telegram sent: {r.status_code}")
        print("[WATCHDOG] ✅ Telegram alert sent")
    except Exception as e:
        log_alert(f"Telegram FAILED: {e}")
        print(f"[WATCHDOG] ❌ Telegram failed: {e}")

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {'alert_sent': False, 'last_alert': None}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def get_last_scan_minutes_ago():
    """Get minutes since last scan - properly handling ET timezone"""
    try:
        result = subprocess.run(
            ['tail', '-200', CRON_LOG],
            capture_output=True, text=True, timeout=5
        )
        for line in reversed(result.stdout.split('\n')):
            if 'Checking Good360' in line:
                import re
                match = re.search(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\]', line)
                if match:
                    # Parse as naive datetime (log timestamps are in ET)
                    last_dt = datetime.strptime(match.group(1), '%Y-%m-%d %H:%M:%S')
                    # Get current ET as naive for comparison
                    now_et = datetime.now(ET).replace(tzinfo=None)
                    minutes_ago = int((now_et - last_dt).total_seconds() / 60)
                    return minutes_ago, match.group(1)
    except Exception as e:
        print(f"[WATCHDOG] Error reading log: {e}")
    return 9999, 'Unknown'

def main():
    now_et = datetime.now(ET)
    print(f"[{now_et.strftime('%Y-%m-%d %H:%M:%S')}] Watchdog checking...")

    minutes_ago, last_time = get_last_scan_minutes_ago()
    state = load_state()

    print(f"[WATCHDOG] Last scan: {last_time} ({minutes_ago} min ago)")

    # Check business hours (Mon-Fri 6AM-11PM ET)
    is_weekday = now_et.weekday() < 5
    is_business_hours = is_weekday and 6 <= now_et.hour < 23

    if not is_business_hours:
        print("[WATCHDOG] Outside business hours - skipping alert check")
        save_state(state)
        return

    if minutes_ago > MAX_STALE_MINUTES:
        if not state.get('alert_sent'):
            msg = f"🚨 GOOD360 MONITOR DOWN\n\n⚠️ Last scan: {minutes_ago} minutes ago\nLast run: {last_time} ET\n\n🔧 System needs attention!\n\n— E-Comsetter Watchdog"
            send_telegram_alert(msg)
            log_alert(f"ALERT SENT: {minutes_ago} min since last scan")
            state['alert_sent'] = True
            state['last_alert'] = now_et.isoformat()
            state['minutes_ago'] = minutes_ago
            save_state(state)
        else:
            print("[WATCHDOG] Alert already sent - waiting for recovery")
    else:
        print(f"[WATCHDOG] ✅ System healthy - last scan {minutes_ago} min ago")
        if state.get('alert_sent'):
            # Recovery!
            msg = f"✅ GOOD360 MONITOR RECOVERED\n\n🟢 System is back online\nLast scan: {minutes_ago} min ago\n\n— E-Comsetter Watchdog"
            send_telegram_alert(msg)
            log_alert("RECOVERY ALERT SENT")
            state['alert_sent'] = False
            state['last_recovery'] = now_et.isoformat()
            save_state(state)

if __name__ == '__main__':
    main()
