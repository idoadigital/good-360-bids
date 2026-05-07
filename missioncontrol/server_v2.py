#!/usr/bin/env python3
"""
E-Comsetter Mission Control API Server v2
Updated to match Base44 Dashboard Integration Specification
"""

import json
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from functools import wraps

from flask import Flask, jsonify, redirect, request, send_from_directory

# Make sibling modules importable when run as `python missioncontrol/server_v2.py`
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth as auth_mod  # noqa: E402
from admin_routes import register_admin  # noqa: E402
from db import has_any_user, init_db  # noqa: E402
import quickbeed  # noqa: E402
import secrets_store  # noqa: E402  (validates DASHBOARD_MASTER_KEY at import via first use)

app = Flask(__name__, static_folder='static')

# Configuration
API_KEY = os.environ.get('MISSIONCONTROL_API_KEY', '')
if not API_KEY:
    raise RuntimeError('MISSIONCONTROL_API_KEY is not set — refuse to start with empty auth')

# Validate the master encryption key at boot so we fail fast.
try:
    _ = secrets_store._load_master_key()  # type: ignore[attr-defined]
except Exception as _exc:
    raise RuntimeError(f'Dashboard cannot start: {_exc}') from _exc

init_db()
WORKDIR = os.environ.get('WORKDIR', '/a0/usr/workdir')
CONFIG_FILE = f'{WORKDIR}/good360_checkout_config.json'
CRON_LOG = f'{WORKDIR}/good360_cron.log'
RUN_LOG = f'{WORKDIR}/good360_run_log.json'
PAUSE_FLAG = f'{WORKDIR}/good360_paused.flag'
MONITOR_SCRIPT = f'{WORKDIR}/good360_monitor.py'
STATE_FILE = f'{WORKDIR}/missioncontrol_state.json'

# System start time for uptime calculation
START_TIME = time.time()

# Authentication decorator.
# A request is authorized if EITHER it carries a valid X-API-Key header (for
# server-to-server callers / scripts) OR it has a valid dashboard session
# cookie (for browser users). Anonymous GETs are no longer permitted — that
# bypass leaked operational data to anyone who could reach the port.
def require_api_key(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get('X-API-Key')
        if api_key and api_key == API_KEY:
            return f(*args, **kwargs)
        if auth_mod.current_user():
            return f(*args, **kwargs)
        return jsonify({
            'success': False,
            'error': {'code': 'UNAUTHORIZED', 'message': 'Invalid or missing credentials'}
        }), 401
    return decorated

# Helper functions
def load_config():
    try:
        with open(CONFIG_FILE) as f:
            return json.load(f)
    except:
        return {}

def save_config(config):
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

def load_run_log():
    """Load run log and return list of runs"""
    try:
        with open(RUN_LOG) as f:
            data = json.load(f)
            # Handle both formats: {"runs": [...]} and just [...]
            if isinstance(data, dict) and 'runs' in data:
                return data.get('runs', [])
            elif isinstance(data, list):
                return data
            else:
                return []
    except:
        return []

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except:
        return {
            'active_orgs': 1,
            'trucks_found': 0,
            'last_scan': None,
            'scan_count': 0,
            'purchases_today': 0,
            'misses_today': 0
        }

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2)

def is_paused():
    return os.path.exists(PAUSE_FLAG)

def get_process_pid(name):
    try:
        result = subprocess.run(
            ['pgrep', '-f', name],
            capture_output=True, text=True
        )
        if result.stdout.strip():
            return int(result.stdout.strip().split('\n')[0])
        return None
    except:
        return None


HEARTBEAT_FILE = f'{WORKDIR}/good360_heartbeat.json'
_MONITOR_STALE_AFTER = timedelta(minutes=int(os.environ.get('MONITOR_STALE_MINUTES', '5')))


def monitor_alive_from_heartbeat():
    # pgrep can't see the monitor process from inside this container (separate
    # PID namespace). Heartbeat freshness is the ground truth for liveness.
    try:
        with open(HEARTBEAT_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return False, None
    raw_ts = data.get('last_success') or data.get('last_scan')
    if not raw_ts:
        return False, None
    try:
        ts = datetime.fromisoformat(raw_ts.replace('Z', '+00:00'))
    except ValueError:
        return False, raw_ts
    now = datetime.now(ts.tzinfo) if ts.tzinfo else datetime.now()
    return (now - ts) < _MONITOR_STALE_AFTER, raw_ts

def is_process_running(name):
    return get_process_pid(name) is not None

def get_cron_log_lines(n=50):
    try:
        with open(CRON_LOG) as f:
            lines = f.readlines()
            return [l.strip() for l in lines[-n:] if l.strip()]
    except:
        return []

def get_next_wednesday():
    """Calculate next Wednesday at 00:00:00 EST"""
    now = datetime.now()
    days_until_wednesday = (2 - now.weekday()) % 7  # Wednesday = 2
    if days_until_wednesday == 0:
        days_until_wednesday = 7  # If today is Wednesday, get next Wednesday
    next_wed = now + timedelta(days=days_until_wednesday)
    next_wed = next_wed.replace(hour=0, minute=0, second=0, microsecond=0)
    return next_wed.isoformat()

def check_stale_lock(lock_acquired_at, timeout_minutes=10):
    """Check if a lock is stale (older than timeout_minutes)"""
    if not lock_acquired_at:
        return False
    try:
        lock_time = datetime.fromisoformat(lock_acquired_at)
        elapsed = (datetime.now() - lock_time).total_seconds() / 60
        return elapsed > timeout_minutes
    except:
        return False

def parse_log_line(line):
    level = 'info'
    if 'ERROR' in line.upper() or 'FAIL' in line.upper():
        level = 'error'
    elif 'WARN' in line.upper():
        level = 'warning'
    elif 'SUCCESS' in line.upper() or '✅' in line:
        level = 'success'
    elif 'MISSED' in line.upper() or '⚠️' in line:
        level = 'warning'

    timestamp = None
    parts = line.split(' ')
    if len(parts) >= 2:
        date_part = parts[0]
        time_part = parts[1] if ':' in parts[1] else None
        if time_part:
            timestamp = f"{date_part} {time_part}"

    return {
        'timestamp': timestamp or datetime.now().isoformat(),
        'level': level,
        'source': 'monitor',
        'message': line
    }

# ============================================
# STATIC FILES
# ============================================

@app.route('/')
def index():
    """Root: send authenticated users to the legacy ops dashboard, anonymous
    users to login (or register if no users exist yet)."""
    if auth_mod.current_user():
        return send_from_directory('static', 'index.html')
    if not has_any_user():
        return redirect('/register')
    return redirect('/login')

@app.route('/static/<path:path>')
def serve_static(path):
    return send_from_directory('static', path)

# Liveness probe — no auth, returns 200/JSON. Kept narrow on purpose.
@app.route('/healthz', methods=['GET'])
def liveness():
    return jsonify({'status': 'ok'})

# ============================================
# API ENDPOINTS - Matching Base44 Spec
# ============================================

@app.route('/api/health', methods=['GET'])
@require_api_key
def health_check():
    """Health check - matches Base44 spec"""
    config = load_config()
    state = load_state()

    # Count trucks today
    run_log = load_run_log()
    today = datetime.now().strftime('%Y-%m-%d')
    trucks_today = [r for r in run_log if r.get('time', '').startswith(today)]

    return jsonify({
        'status': 'ok',
        'active_orgs': state.get('active_orgs', 1),
        'trucks_found': state.get('trucks_found', len(trucks_today)),
        'last_scan': state.get('last_scan'),
        'uptime': int(time.time() - START_TIME)
    })

@app.route('/api/status', methods=['GET'])
@require_api_key
def get_status():
    """Detailed system status - matches Base44 spec"""
    config = load_config()
    state = load_state()

    # Liveness signals. Monitor lives in a sibling container, so pgrep can't
    # see it — use heartbeat freshness instead. Bot/watchdog have no heartbeat
    # yet; their pgrep checks will always be False in containerized deploys
    # but are reported separately for visibility, not gating system status.
    monitor_running, heartbeat_ts = monitor_alive_from_heartbeat()
    bot_running = is_process_running('good360_telegram_bot.py')
    watchdog_running = is_process_running('good360_watchdog.py')

    # Prefer the heartbeat timestamp over state.last_scan when available.
    last_scan = heartbeat_ts or state.get('last_scan')

    # Count today's scans
    run_log = load_run_log()
    today = datetime.now().strftime('%Y-%m-%d')
    scans_today = len([r for r in run_log if r.get('time', '').startswith(today)])

    # Check cooldown
    cooldown_until = config.get('org_cooldown', {}).get('hope4humanity', {}).get('cooldown_until')
    cooldown_active = False
    if cooldown_until:
        try:
            cooldown_date = datetime.fromisoformat(cooldown_until)
            cooldown_active = datetime.now() < cooldown_date
        except:
            pass

    # System status is driven by the monitor — that's the core function. Bot
    # status is reported separately. Without a cross-container liveness probe
    # for the bot we can't reliably degrade on it from in here.
    system_status = 'healthy' if monitor_running else 'down'

    return jsonify({
        'system': {
            'status': system_status,
            'uptime_hours': round((time.time() - START_TIME) / 3600, 2)
        },
        'monitor': {
            'running': monitor_running,
            'last_scan': last_scan,
            'scans_today': scans_today
        },
        'autobuy': {
            'enabled': config.get('autobuy_enabled', True),
            'paused': is_paused(),
            'cooldown_active': cooldown_active
        },
        'telegram_bot': {
            'running': bot_running,
            'connected': bot_running
        },
        'watchdog': {
            'running': watchdog_running
        }
    })

@app.route('/api/pause', methods=['POST'])
@require_api_key
def pause_autobuy():
    """Pause all auto-buy scanning"""
    data = request.get_json() or {}
    reason = data.get('reason', 'Manual pause via API')

    with open(PAUSE_FLAG, 'w') as f:
        f.write(json.dumps({
            'paused_at': datetime.now().isoformat(),
            'reason': reason,
            'paused_by': 'api'
        }))

    # Log activity
    log_activity({
        'event_type': 'auto_buy_off',
        'title': 'Auto-Buy Paused',
        'message': reason,
        'severity': 'warning'
    })

    return jsonify({
        'success': True,
        'message': 'Auto-buy paused successfully'
    })

@app.route('/api/resume', methods=['POST'])
@require_api_key
def resume_autobuy():
    """Resume auto-buy scanning"""
    if os.path.exists(PAUSE_FLAG):
        os.remove(PAUSE_FLAG)

    # Log activity
    log_activity({
        'event_type': 'auto_buy_on',
        'title': 'Auto-Buy Resumed',
        'message': 'Auto-buy scanning resumed',
        'severity': 'info'
    })

    return jsonify({
        'success': True,
        'message': 'Auto-buy resumed successfully'
    })

@app.route('/api/test', methods=['POST'])
@require_api_key
def trigger_test():
    """Trigger a single test scan cycle"""
    try:
        subprocess.Popen(
            ['python3', MONITOR_SCRIPT],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )
        return jsonify({
            'success': True,
            'message': 'Test scan triggered'
        })
    except Exception as e:
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500

@app.route('/api/trucks', methods=['GET'])
@require_api_key
def get_trucks():
    """List detected trucks"""
    run_log = load_run_log()
    limit = request.args.get('limit', 50, type=int)

    # Transform to match expected format
    trucks = []
    for entry in run_log[-limit:]:
        truck = {
            'id': entry.get('id', entry.get('truck_id', 'unknown')),
            'org_id': entry.get('org_id', 'hope4humanity'),
            'title': entry.get('title', entry.get('truck_title', 'Unknown Truck')),
            'good360_truck_id': entry.get('good360_truck_id', ''),
            'url': entry.get('url', ''),
            'status': entry.get('status', 'detected'),
            'price': entry.get('price', 0),
            'category': entry.get('category', 'other'),
            'location': entry.get('location', 'Unknown'),
            'detected_at': entry.get('time', entry.get('detected_at')),
            'purchased_at': entry.get('purchased_at'),
            'confirmation_number': entry.get('confirmation_number'),
            'missed_reason': entry.get('missed_reason')
        }
        trucks.append(truck)

    return jsonify({
        'success': True,
        'data': trucks
    })

@app.route('/api/logs', methods=['GET'])
@require_api_key
def get_logs():
    """Activity/event logs"""
    limit = request.args.get('limit', 50, type=int)

    # Load activity log file if exists
    activity_log_file = f'{WORKDIR}/activity_log.json'
    try:
        with open(activity_log_file) as f:
            logs = json.load(f)
    except:
        logs = []

    # Also parse cron logs
    raw_logs = get_cron_log_lines(limit)
    for log in raw_logs:
        parsed = parse_log_line(log)
        logs.append(parsed)

    return jsonify({
        'success': True,
        'data': logs[-limit:]
    })

@app.route('/api/alerts', methods=['GET'])
@require_api_key
def get_alerts():
    """Alert notifications"""
    limit = request.args.get('limit', 20, type=int)

    # Parse logs for alerts
    logs = get_cron_log_lines(200)
    alerts = []

    for log in logs:
        if any(x in log for x in ['✅', '⚠️', '❌', 'AUTO-BUY', 'TRUCK MISSED', 'COMPLETE', 'FAILED']):
            alert_type = 'info'
            if '✅' in log or 'COMPLETE' in log:
                alert_type = 'success'
            elif '⚠️' in log or 'MISSED' in log:
                alert_type = 'missed'
            elif '❌' in log or 'FAILED' in log:
                alert_type = 'failed'

            alerts.append({
                'type': alert_type,
                'message': log,
                'timestamp': datetime.now().isoformat()
            })

    return jsonify({
        'success': True,
        'data': alerts[-limit:]
    })

@app.route('/api/transactions', methods=['GET'])
@require_api_key
def get_transactions():
    """Purchase history"""
    limit = request.args.get('limit', 20, type=int)

    run_log = load_run_log()
    transactions = []

    for entry in run_log:
        if entry.get('status') in ['purchased', 'failed', 'missed']:
            transactions.append({
                'id': entry.get('id', 'unknown'),
                'org_id': entry.get('org_id', 'hope4humanity'),
                'truck_title': entry.get('title', 'Unknown'),
                'truck_price': entry.get('price', 0),
                'admin_fee': entry.get('admin_fee', 0),
                'shipping_fee': entry.get('shipping_fee', 0),
                'total': entry.get('total', entry.get('price', 0)),
                'status': entry.get('status'),
                'started_at': entry.get('time'),
                'completed_at': entry.get('purchased_at'),
                'confirmation_number': entry.get('confirmation_number'),
                'payment_method': entry.get('payment_method', 'Visa ending 7421')
            })

    return jsonify({
        'success': True,
        'data': transactions[-limit:]
    })

@app.route('/api/cooldown', methods=['GET'])
@require_api_key
def get_cooldown():
    """Cooldown status for all orgs"""
    config = load_config()
    cooldown_info = config.get('org_cooldown', {}).get('hope4humanity', {})

    cooldown_until = cooldown_info.get('cooldown_until')
    last_purchase = cooldown_info.get('last_purchase_date')

    cooldown_active = False
    days_remaining = 0

    if cooldown_until:
        try:
            cooldown_date = datetime.fromisoformat(cooldown_until)
            cooldown_active = datetime.now() < cooldown_date
            if cooldown_active:
                days_remaining = (cooldown_date - datetime.now()).days
        except:
            pass

    return jsonify({
        'success': True,
        'data': {
            'cooldown_active': cooldown_active,
            'org_id': 'hope4humanity',
            'org_name': 'Hope 4 Humanity',
            'last_purchase_date': last_purchase,
            'cooldown_until': cooldown_until,
            'cooldown_type': 'calendar_week',
            'days_remaining': days_remaining,
            'can_purchase': not cooldown_active
        }
    })

@app.route('/api/config', methods=['GET'])
@require_api_key
def get_config():
    """Get current configuration"""
    config = load_config()

    return jsonify({
        'success': True,
        'data': {
            'org': {
                'id': 'hope4humanity',
                'name': 'Hope 4 Humanity',
                'email': config.get('username', 'berneitha@hope4humanity.us'),
                'warehouse': config.get('warehouse', '1025 Progress Circle, Lawrenceville, GA 30043')
            },
            'autobuy': {
                'enabled': config.get('autobuy_enabled', True),
                'max_price': config.get('max_auto_pay', 6400)
            },
            'targets': config.get('autobuy_targets', []),
            'excluded': config.get('autobuy_exclude', []),
            'schedule': {
                'scan_interval_minutes': 1,
                'business_hours_start': '06:00',
                'business_hours_end': '23:00',
                'timezone': 'America/New_York',
                'days': 'Monday-Friday'
            },
            'alerts': {
                'telegram_enabled': True,
                'email_enabled': True,
                'email_recipients': ['berneitha@hope4humanity.us', 'sdibao@gmail.com']
            }
        }
    })

@app.route('/api/config', methods=['PUT'])
@require_api_key
def update_config():
    """Update configuration"""
    config = load_config()
    data = request.get_json() or {}

    changes = []

    if 'max_price' in data:
        old = config.get('max_auto_pay', 6400)
        config['max_auto_pay'] = data['max_price']
        changes.append(f'max_price: {old} → {data["max_price"]}')

    if 'targets' in data:
        config['autobuy_targets'] = data['targets']
        changes.append('targets updated')

    if 'enabled' in data:
        config['autobuy_enabled'] = data['enabled']
        changes.append(f'autobuy_enabled: {data["enabled"]}')

    save_config(config)

    return jsonify({
        'success': True,
        'message': 'Configuration updated successfully',
        'changes': changes
    })

@app.route('/api/force-buy', methods=['POST'])
@require_api_key
def force_buy():
    """Force purchase for a specific org/truck"""
    data = request.get_json() or {}
    org_id = data.get('org_id', 'hope4humanity')
    truck_id = data.get('truck_id')
    truck_url = data.get('truck_url')

    if not truck_id and not truck_url:
        return jsonify({
            'success': False,
            'error': 'truck_id or truck_url is required'
        }), 400

    # Log activity
    log_activity({
        'event_type': 'info',
        'org_id': org_id,
        'title': 'Force Buy Triggered',
        'message': f'Manual purchase triggered for {truck_id or truck_url}',
        'severity': 'info'
    })

    return jsonify({
        'success': True,
        'data': {
            'purchase_id': f'force_{datetime.now().strftime("%Y%m%d_%H%M%S")}',
            'status': 'initiated',
            'org_id': org_id,
            'message': 'Force purchase initiated. Monitor Telegram for updates.'
        }
    })

# ============================================
# ACTIVITY LOG HELPER
# ============================================

def log_activity(entry):
    """Log an activity event"""
    activity_log_file = f'{WORKDIR}/activity_log.json'

    try:
        with open(activity_log_file) as f:
            logs = json.load(f)
    except:
        logs = []

    entry['timestamp'] = datetime.now().isoformat()
    logs.append(entry)

    # Keep last 1000 entries
    logs = logs[-1000:]

    with open(activity_log_file, 'w') as f:
        json.dump(logs, f, indent=2)

# ============================================
# ADMIN DASHBOARD (auth, users, settings, log views)
# ============================================
register_admin(app)


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    resp.headers.setdefault(
        "Strict-Transport-Security",
        "max-age=63072000; includeSubDomains",
    )
    return resp


def _ssl_context():
    """Return Flask ssl_context tuple if a cert pair exists; else None."""
    cert = os.environ.get("DASHBOARD_TLS_CERT", "/app/workdir/tls/dashboard.crt")
    key = os.environ.get("DASHBOARD_TLS_KEY", "/app/workdir/tls/dashboard.key")
    if os.path.exists(cert) and os.path.exists(key):
        return (cert, key)
    return None


def _start_quickbeed_poll_thread():
    """Background fallback for missed webhooks. Runs forever; survives bad
    QuickBeed config (just sleeps until config is set via the Settings UI)."""
    import threading
    import time as _time

    def loop():
        while True:
            try:
                cfg = quickbeed.get_config()
                interval = cfg["poll_interval_s"]
            except quickbeed.QuickBeedConfigError:
                _time.sleep(60)
                continue
            try:
                quickbeed.incremental_sync()
            except Exception:
                import traceback
                traceback.print_exc()
            _time.sleep(interval)

    t = threading.Thread(target=loop, name="quickbeed-poll", daemon=True)
    t.start()
    print('🔄 QuickBeed poll thread started (incremental_sync every QUICKBEED_POLL_INTERVAL_SECONDS)')


if __name__ == '__main__':
    _start_quickbeed_poll_thread()
    ssl_ctx = _ssl_context()
    scheme = 'https' if ssl_ctx else 'http'
    print('🚀 Starting Mission Control + Admin Dashboard...')
    print(f'📊 Dashboard: {scheme}://0.0.0.0:5001')
    print(f'🔗 API Base:  {scheme}://0.0.0.0:5001/api')
    print(f'🔑 API Key (server-to-server): {API_KEY[:4]}…{API_KEY[-4:]} (len={len(API_KEY)})')
    if ssl_ctx:
        print(f'🔒 TLS: {ssl_ctx[0]}')
    else:
        print('⚠️  TLS cert not found — running plain HTTP. Set DASHBOARD_TLS_CERT/KEY for HTTPS.')
    app.run(host='0.0.0.0', port=5001, debug=False, ssl_context=ssl_ctx)
