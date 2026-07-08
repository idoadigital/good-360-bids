#!/usr/bin/env python3
"""Self-heal incident responder — a strictly bounded playbook, NOT a general agent.

Runs as its own compose service and loops every SELF_HEAL_INTERVAL_SECONDS
(default 60s). Every check is independently try/excepted so one failure never
kills the loop; every action raises an ADMIN Telegram alert via
telegram_router (which also records each send — delivered or gated — in the
notifications log); every decision is printed to stdout for `docker logs`.

Playbook (the ONLY things this process is allowed to do):
  1. External liveness: overall health = monitor heartbeat fresh (per
     healthcheck.py's logic) AND daemon /health answering 200 within 10s.
     Healthy -> GET HEALTHCHECKS_PING_URL; unhealthy -> GET <url>/fail so
     healthchecks.io pages the operator externally. No URL configured ->
     skip silently; healthchecks.io being down is ignored.
  2. Bookkeeping repair: purchase_attempts rows stuck status='in_progress'
     for more than 2 hours are closed to status='failed_checkout' (status and
     error_message only; younger rows and all other columns are untouched).
  3. Daemon wedge: /health failing 3 consecutive cycles -> docker restart of
     the daemon container, at most once per 30 minutes.
  4. Scanner stale: monitor heartbeat older than 10 minutes -> docker restart
     of the monitor container, at most once per 30 minutes (tracked
     separately). Individual scan errors self-recover on the next cycle and
     never trigger a restart — only a stale heartbeat does.
  5. Error burst (alert only): more than 8 monitor CRITICAL ERROR
     notifications in the last hour -> "investigate" alert, no action taken,
     at most once per 2 hours.

HARD BOUNDARIES — self-heal may restart the two named containers, close
stale bookkeeping rows as described above, and send ADMIN alerts. It must
NEVER: touch payment or card data, modify queue/rotation/cooldown state,
re-enable suspended customers, place or abort purchases, restart any other
container, or edit configuration. Anything outside this playbook -> ADMIN
alert and stand down.

SELF_HEAL_DRY_RUN=true (staging/feature): log + alert as usual but perform
NO container restarts and NO database writes.
"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import traceback
from datetime import datetime, timedelta, timezone

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import healthcheck  # single source of truth for heartbeat path + freshness

INTERVAL_S = int(os.environ.get("SELF_HEAL_INTERVAL_SECONDS", "60"))
WORKDIR = os.environ.get("WORKDIR", "/app/workdir")
STATE_FILE = os.path.join(WORKDIR, "self_heal_state.json")
ROSTER_DB = os.environ.get("ROSTER_DB_PATH", "/app/good360_roster/db/roster.db")
DAEMON_CONTAINER = os.environ.get(
    "SELF_HEAL_DAEMON_CONTAINER", "good-360-bids-daemon-1")
MONITOR_CONTAINER = os.environ.get(
    "SELF_HEAL_MONITOR_CONTAINER", "good-360-bids-monitor-1")
DAEMON_HEALTH_URL = os.environ.get(
    "SELF_HEAL_DAEMON_HEALTH_URL", "http://daemon:5002/health")

STALE_ATTEMPT_HOURS = 2        # playbook item 2
DAEMON_FAIL_CYCLES = 3         # playbook item 3
MONITOR_STALE_MINUTES = 10     # playbook item 4
RESTART_MIN_GAP_S = 30 * 60    # one restart per container per 30 minutes
BURST_THRESHOLD_PER_HOUR = 8   # playbook item 5: alert when count exceeds this
BURST_ALERT_MIN_GAP_S = 2 * 60 * 60


def _dry_run() -> bool:
    return (os.environ.get("SELF_HEAL_DRY_RUN", "") or "").strip().lower() in (
        "1", "true", "yes", "on")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(raw):
    """UTC datetime from a sqlite/ISO timestamp string, or None.
    Naive values are treated as UTC (sqlite datetime('now') is UTC)."""
    if not raw:
        return None
    try:
        ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def alert(message, *, level="warn", title=None):
    """ADMIN Telegram alert. The router gates on ENABLE_NOTIFICATIONS itself
    and records every send (delivered or gated) in the notifications log."""
    print(f"[SELF-HEAL] ALERT: {message}")
    try:
        import telegram_router
        telegram_router.send(telegram_router.ADMIN, message,
                             source="self_heal", level=level, title=title)
    except Exception as e:
        print(f"[SELF-HEAL] alert send failed (ignored): {e}")


# ---------------------------------------------------------------------------
# State file (restart / alert rate-limit timestamps)
# ---------------------------------------------------------------------------

def load_state() -> dict:
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"[SELF-HEAL] could not write state file: {e}")


def _rate_limit_ok(state: dict, key: str, min_gap_s: int) -> bool:
    last = _parse_ts(state.get(key))
    if last is None:
        return True
    return (_now_utc() - last).total_seconds() >= min_gap_s


# ---------------------------------------------------------------------------
# Probes
# ---------------------------------------------------------------------------

def probe_daemon():
    """(healthy, detail) for the daemon's own /health endpoint."""
    try:
        resp = requests.get(DAEMON_HEALTH_URL, timeout=10)
        if resp.status_code == 200:
            return True, "HTTP 200"
        return False, f"HTTP {resp.status_code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def heartbeat_age_minutes():
    """Age of the monitor heartbeat in minutes, or None when the file is
    missing/unreadable. Path, field precedence and tz handling follow
    healthcheck.py (its check_heartbeat covers only the 5-minute freshness
    verdict; item 4 needs the raw age for its own 10-minute threshold)."""
    try:
        data = json.loads(healthcheck.HEARTBEAT_FILE.read_text())
        ts = _parse_ts(data.get("last_scan") or data.get("last_success"))
        if ts is None:
            return None
        return (_now_utc() - ts).total_seconds() / 60.0
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Playbook item 1: external liveness (deadman replacement)
# ---------------------------------------------------------------------------

def check_external_liveness(daemon_ok: bool) -> None:
    url = (os.environ.get("HEALTHCHECKS_PING_URL") or "").strip()
    if not url:
        return  # not configured -> skip silently
    hb_ok, hb_msg = healthcheck.check_heartbeat()
    healthy = hb_ok and daemon_ok
    print(f"[SELF-HEAL] liveness: heartbeat={'ok' if hb_ok else hb_msg!r} "
          f"daemon={'ok' if daemon_ok else 'failing'} -> "
          f"ping {'alive' if healthy else 'FAIL'}")
    try:
        requests.get(url if healthy else url + "/fail", timeout=10)
    except Exception as e:
        # healthchecks.io being down must never matter here.
        print(f"[SELF-HEAL] liveness ping failed (ignored): {e}")


# ---------------------------------------------------------------------------
# Playbook item 2: close stale in_progress purchase attempts (bookkeeping)
# ---------------------------------------------------------------------------

def check_stale_attempts(state: dict | None = None) -> None:
    if not os.path.exists(ROSTER_DB):
        print(f"[SELF-HEAL] roster db not found ({ROSTER_DB}) — skipping")
        return
    cutoff = _now_utc() - timedelta(hours=STALE_ATTEMPT_HOURS)
    conn = sqlite3.connect(ROSTER_DB, timeout=10)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, started_at FROM purchase_attempts "
            "WHERE status = 'in_progress'").fetchall()
        stale = [(r["id"], r["started_at"]) for r in rows
                 if (_parse_ts(r["started_at"]) or _now_utc()) < cutoff]
        for att_id, started_at in stale:
            note = (f"[self-heal] stale in_progress attempt closed "
                    f"(started {started_at}, closed after "
                    f">{STALE_ATTEMPT_HOURS}h)")
            if _dry_run():
                print(f"[SELF-HEAL] DRY RUN: would close purchase_attempts "
                      f"id={att_id} (started {started_at})")
                continue
            conn.execute(
                "UPDATE purchase_attempts "
                "SET status = 'failed_checkout', error_message = ? "
                "WHERE id = ? AND status = 'in_progress'",
                (note, att_id))
            conn.commit()
            print(f"[SELF-HEAL] closed stale purchase_attempts id={att_id} "
                  f"(started {started_at})")
        if stale:
            ids = ", ".join(str(i) for i, _ in stale)
            suffix = " [DRY RUN — no DB write]" if _dry_run() else ""
            # Rate-limit the ALERT (not the closes): should the row close
            # fail — or in dry-run, where nothing closes — the same rows
            # stay stale every cycle and would spam admin once a minute.
            if state is None or _rate_limit_ok(state, "last_stale_alert", 2 * 3600):
                alert(f"🩹 Self-heal: closed {len(stale)} stale in_progress "
                      f"purchase attempt(s) (older than {STALE_ATTEMPT_HOURS}h): "
                      f"id(s) {ids}{suffix}",
                      level="warn", title="Self-heal: stale attempts closed")
                if state is not None:
                    state["last_stale_alert"] = _now_utc().isoformat()
                    save_state(state)
            else:
                print(f"[SELF-HEAL] stale-attempt alert suppressed "
                      f"(rate limit): id(s) {ids}")
        else:
            print("[SELF-HEAL] no stale in_progress purchase attempts")
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Container restarts (playbook items 3 + 4)
# ---------------------------------------------------------------------------

def _docker_restart(name):
    """`docker restart <name>` with the image's docker CLI over the mounted
    /var/run/docker.sock. Returns (ok, output); never raises."""
    try:
        res = subprocess.run(["docker", "restart", name],
                             capture_output=True, text=True, timeout=120)
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        return res.returncode == 0, out
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _restart_container(name, state, state_key, alert_msg, title):
    # Record the attempt timestamp first (also in dry run) so a broken
    # restart path can't fire more than once per window.
    state[state_key] = _now_utc().isoformat(timespec="seconds")
    save_state(state)
    if _dry_run():
        print(f"[SELF-HEAL] DRY RUN: would run docker restart {name}")
        alert(alert_msg + "\n[DRY RUN — restart NOT performed]",
              level="warn", title=title)
        return
    ok, out = _docker_restart(name)
    if ok:
        print(f"[SELF-HEAL] restarted container {name}")
        alert(alert_msg, level="warn", title=title)
    else:
        print(f"[SELF-HEAL] docker restart {name} FAILED: {out}")
        alert(alert_msg + f"\n❌ RESTART FAILED: {out}",
              level="error", title=title)


def check_daemon_wedge(state, daemon_ok, detail, fail_history) -> None:
    """Playbook item 3: restart the daemon after 3 consecutive bad probes."""
    if daemon_ok:
        if fail_history:
            print(f"[SELF-HEAL] daemon /health recovered after "
                  f"{len(fail_history)} bad cycle(s)")
        fail_history.clear()
        return
    fail_history.append(detail)
    print(f"[SELF-HEAL] daemon /health FAIL "
          f"({len(fail_history)} consecutive): {detail}")
    if len(fail_history) < DAEMON_FAIL_CYCLES:
        return
    if not _rate_limit_ok(state, "last_daemon_restart", RESTART_MIN_GAP_S):
        print(f"[SELF-HEAL] daemon restart due but suppressed "
              f"(rate limit: one per {RESTART_MIN_GAP_S // 60}m)")
        return
    errs = "\n".join(f"- {e}" for e in fail_history[-DAEMON_FAIL_CYCLES:])
    _restart_container(
        DAEMON_CONTAINER, state, "last_daemon_restart",
        f"🔄 Self-heal: daemon /health failed {len(fail_history)} consecutive "
        f"cycles — restarting {DAEMON_CONTAINER}\nProbe errors:\n{errs}",
        "Self-heal: daemon restart")
    fail_history.clear()


def check_monitor_stale(state) -> None:
    """Playbook item 4: restart the monitor when its heartbeat is stale."""
    age = heartbeat_age_minutes()
    if age is not None and age <= MONITOR_STALE_MINUTES:
        return
    # Missing/unreadable heartbeat counts as stale, same as healthcheck.py.
    desc = ("heartbeat missing/unreadable" if age is None
            else f"heartbeat {age:.0f} min old")
    print(f"[SELF-HEAL] monitor stale: {desc} "
          f"(limit {MONITOR_STALE_MINUTES}m)")
    if not _rate_limit_ok(state, "last_monitor_restart", RESTART_MIN_GAP_S):
        print(f"[SELF-HEAL] monitor restart due but suppressed "
              f"(rate limit: one per {RESTART_MIN_GAP_S // 60}m)")
        return
    _restart_container(
        MONITOR_CONTAINER, state, "last_monitor_restart",
        f"🔄 Self-heal: scan {desc} — restarting {MONITOR_CONTAINER}",
        "Self-heal: monitor restart")


# ---------------------------------------------------------------------------
# Playbook item 5: monitor error burst (alert only, no action)
# ---------------------------------------------------------------------------

def check_error_burst(state) -> None:
    try:
        import notifications_log
        get_conn = notifications_log._get_conn()
    except Exception:
        get_conn = None
    if get_conn is None:
        return  # dashboard db unreachable — nothing to count
    cutoff = _now_utc() - timedelta(hours=1)
    with get_conn() as c:
        rows = c.execute(
            "SELECT ts FROM notifications "
            "WHERE source = 'monitor' AND level = 'error' "
            "AND message LIKE '%CRITICAL ERROR%' "
            "ORDER BY id DESC LIMIT 500").fetchall()
    count = sum(1 for r in rows
                if (_parse_ts(r["ts"]) or cutoff) > cutoff)
    if count <= BURST_THRESHOLD_PER_HOUR:
        return
    print(f"[SELF-HEAL] scanner error burst: {count} monitor CRITICAL "
          f"ERRORs in the last hour (threshold {BURST_THRESHOLD_PER_HOUR})")
    if not _rate_limit_ok(state, "last_burst_alert", BURST_ALERT_MIN_GAP_S):
        print("[SELF-HEAL] burst alert suppressed (rate limit: one per 2h)")
        return
    state["last_burst_alert"] = _now_utc().isoformat(timespec="seconds")
    save_state(state)
    alert(f"⚠️ Self-heal: scanner error burst — {count} monitor CRITICAL "
          f"ERRORs in the last hour — investigate (no automatic action "
          f"taken)", level="warn", title="Self-heal: scanner error burst")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_cycle(state, daemon_fail_history) -> None:
    """One pass over the playbook; each check individually guarded."""
    try:
        daemon_ok, daemon_detail = probe_daemon()
    except Exception as e:  # probe_daemon never raises, but stay paranoid
        daemon_ok, daemon_detail = False, f"probe crashed: {e}"
    checks = (
        ("external-liveness", lambda: check_external_liveness(daemon_ok)),
        ("stale-attempts", lambda: check_stale_attempts(state)),
        ("daemon-wedge", lambda: check_daemon_wedge(
            state, daemon_ok, daemon_detail, daemon_fail_history)),
        ("monitor-stale", lambda: check_monitor_stale(state)),
        ("error-burst", lambda: check_error_burst(state)),
    )
    for name, fn in checks:
        try:
            fn()
        except Exception:
            print(f"[SELF-HEAL] check '{name}' crashed (loop continues):\n"
                  f"{traceback.format_exc()}")


def main() -> None:
    print(f"[SELF-HEAL] starting: interval={INTERVAL_S}s "
          f"dry_run={_dry_run()} daemon={DAEMON_CONTAINER} "
          f"monitor={MONITOR_CONTAINER} "
          f"hc_ping={'set' if os.environ.get('HEALTHCHECKS_PING_URL') else 'unset'}")
    state = load_state()
    daemon_fail_history: list[str] = []
    while True:
        run_cycle(state, daemon_fail_history)
        time.sleep(INTERVAL_S)


if __name__ == "__main__":
    main()
