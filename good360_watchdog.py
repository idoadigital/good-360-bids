#!/usr/bin/env python3
"""Good360 Watchdog - FIXED VERSION with proper ET timezone handling"""
import json
import os
from datetime import datetime

import pytz

import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox  # noqa: E402  (sandbox-mode alert prefix)
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

# Config
# All state lives in the shared workdir volume so the watchdog (which has no
# access to the monitor container's filesystem) can read what monitor writes.
WORKDIR = os.environ.get('WORKDIR', '/app/workdir')
STATE_FILE = f'{WORKDIR}/good360_watchdog_state.json'
HEARTBEAT_FILE = f'{WORKDIR}/good360_heartbeat.json'
ALERT_LOG = f'{WORKDIR}/good360_watchdog_alerts.log'
MAX_STALE_MINUTES = 5
ET = pytz.timezone('America/New_York')

def log_alert(msg):
    with open(ALERT_LOG, 'a') as f:
        f.write(f"[{datetime.now(ET).strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")

def send_telegram_alert(message):
    """Monitor-down/recovered alerts — admin channels via the router.
    (Previously mislabeled 'operator' but sent to an org group.)"""
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("telegram"))
        return
    message = sandbox.decorate_alert(message)
    try:
        import telegram_router
        if telegram_router.send(telegram_router.ADMIN, message, source='watchdog'):
            log_alert("Telegram sent")
            print("[WATCHDOG] ✅ Telegram alert sent")
        else:
            log_alert("Telegram not delivered (see notifications log)")
            print("[WATCHDOG] ❌ Telegram not delivered")
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
    """Read minutes-since-last-scan from the heartbeat file.

    The monitor writes good360_heartbeat.json to the shared workdir volume on
    every successful scan. Older versions of this watchdog tailed
    good360_cron.log, which lived only in the monitor container's ephemeral
    filesystem and was therefore unreadable from here — leading to permanent
    "Unknown / 9999 min ago" reports.
    """
    try:
        with open(HEARTBEAT_FILE) as f:
            data = json.load(f)
        raw_ts = data.get('last_success') or data.get('last_scan')
        if not raw_ts:
            return 9999, 'Unknown'
        ts = datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
        if ts.tzinfo is None:
            ts = ET.localize(ts)
        ts_et = ts.astimezone(ET)
        now_et = datetime.now(ET)
        minutes_ago = int((now_et - ts_et).total_seconds() / 60)
        return minutes_ago, ts_et.strftime('%Y-%m-%d %H:%M:%S')
    except FileNotFoundError:
        print(f"[WATCHDOG] Heartbeat file missing: {HEARTBEAT_FILE}")
    except Exception as e:
        print(f"[WATCHDOG] Error reading heartbeat: {e}")
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
    # Long-lived loop. Previously this script exited after each check and
    # relied on `restart: always` to re-launch — wasteful and made the
    # container's "Restarting" status indistinguishable from a real fault.
    import time
    CHECK_INTERVAL = int(os.environ.get('WATCHDOG_INTERVAL_SECONDS', '60'))
    while True:
        try:
            main()
        except KeyboardInterrupt:
            print('[WATCHDOG] Interrupted, exiting')
            break
        except Exception as e:
            print(f'[WATCHDOG] Unhandled error: {e}')
        time.sleep(CHECK_INTERVAL)
