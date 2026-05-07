"""Admin dashboard routes: auth, user mgmt, settings, log views.

Mounted onto the existing Flask app from server_v2.py via register_admin().
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone

UTC = timezone.utc
from pathlib import Path

import csv
import hashlib
import hmac
import io
import threading

from flask import Blueprint, Response, jsonify, redirect, request, send_from_directory

import auth
from db import count_users, get_conn, has_any_user
import quickbeed
import secrets_store

bp = Blueprint("admin", __name__)

WORKDIR = os.environ.get("WORKDIR", "/app/workdir")
ENV_FILE = Path(os.environ.get("DASHBOARD_ENV_FILE", "/app/.env"))
RUN_LOG = f"{WORKDIR}/good360_run_log.json"
HEARTBEAT = f"{WORKDIR}/good360_heartbeat.json"
AUDIT_DIR = Path(os.environ.get("AUDIT_LOG_DIR", f"{WORKDIR}/audit"))
ACTIVITY_LOG = f"{WORKDIR}/activity_log.json"

# Settings keys we manage. The values that actually go into .env when "Apply"
# is clicked are these — anything else in .env is left alone.
SECRET_SETTINGS = {
    # Master scan credentials (used by monitor to log into Good360 for browsing)
    "SCAN_GOOD360_EMAIL", "SCAN_GOOD360_PASSWORD",
    # Per-org Good360 accounts — kept for backwards compat with the legacy
    # multi-org config; can be left empty when QuickBeed is the source of truth.
    "GOOD360_REVIVING_HOMES_EMAIL", "GOOD360_REVIVING_HOMES_PASSWORD",
    "GOOD360_HOPE4HUMANITY_EMAIL", "GOOD360_HOPE4HUMANITY_PASSWORD",
    # Cards
    "CARD_REVIVING_HOMES_NAME", "CARD_REVIVING_HOMES_NUMBER",
    "CARD_REVIVING_HOMES_EXPIRY", "CARD_REVIVING_HOMES_CVV",
    "CARD_HOPE4HUMANITY_NAME", "CARD_HOPE4HUMANITY_NUMBER",
    "CARD_HOPE4HUMANITY_EXPIRY", "CARD_HOPE4HUMANITY_CVV",
    # Telegram
    "TELEGRAM_BOT_TOKEN",
    # SMTP
    "SMTP_PASSWORD",
    # MissionControl
    "MISSIONCONTROL_API_KEY",
    # OpenAI / DevTools
    "OPENAI_API_KEY",
    # QuickBeed customer-sync (the secret bits)
    "QUICKBEED_API_TOKEN", "QUICKBEED_WEBHOOK_SECRET",
}
PUBLIC_SETTINGS = {
    "CARD_REVIVING_HOMES_TYPE", "CARD_HOPE4HUMANITY_TYPE",
    "TELEGRAM_GROUP_REVIVING_HOMES", "TELEGRAM_GROUP_HOPE4HUMANITY", "TELEGRAM_OPERATOR_CHAT_ID",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO",
    "TZ", "LOG_LEVEL", "WORKDIR",
    "AUTOBUY_ENGINE", "DEVTOOLS_AGENT_MODEL", "DEVTOOLS_AGENT_DRY_RUN",
    "DEVTOOLS_AGENT_TIMEOUT_SECONDS", "DEVTOOLS_AGENT_ISOLATED",
    "DEVTOOLS_AGENT_FALLBACK_ON_FAILED", "DEVTOOLS_CHROME_EXECUTABLE",
    "DEVTOOLS_AGENT_ALLOW_SECRETS_TO_MODEL", "DEVTOOLS_AGENT_ALLOW_LIVE_PURCHASE",
    # QuickBeed customer-sync (non-secret bits)
    "QUICKBEED_BASE_URL", "QUICKBEED_APP_ID", "QUICKBEED_CONSUMER_ID",
    "QUICKBEED_POLL_INTERVAL_SECONDS", "QUICKBEED_DRY_RUN",
}
ALL_SETTINGS = SECRET_SETTINGS | PUBLIC_SETTINGS

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ============================================================
# Static pages
# ============================================================

@bp.route("/login")
def login_page():
    return send_from_directory("static", "login.html")


@bp.route("/register")
def register_page():
    if has_any_user():
        return redirect("/login")
    return send_from_directory("static", "register.html")


@bp.route("/admin")
@auth.login_required
def admin_page():
    return send_from_directory("static", "admin.html")


# ============================================================
# Auth
# ============================================================

@bp.route("/api/auth/state", methods=["GET"])
def auth_state():
    """Lightweight: who am I, and is registration open?"""
    u = auth.current_user()
    return jsonify({
        "authenticated": bool(u),
        "user": u,
        "registration_open": not has_any_user(),
        "user_count": count_users(),
    })


@bp.route("/api/auth/register", methods=["POST"])
def register():
    if has_any_user():
        return jsonify({"success": False, "error": "registration closed"}), 403
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    pw = data.get("password") or ""
    if not EMAIL_RE.match(email):
        return jsonify({"success": False, "error": "invalid email"}), 400
    if len(pw) < 12:
        return jsonify({"success": False, "error": "password must be at least 12 characters"}), 400
    try:
        uid = auth.create_user(email, pw, role="super_admin")
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400

    token = auth.issue_session(uid, request.remote_addr or "?", request.headers.get("User-Agent", ""))
    resp = jsonify({"success": True, "user": {"id": uid, "email": email, "role": "super_admin"}})
    _set_session_cookie(resp, token)
    # Bootstrap audit
    with get_conn() as c:
        c.execute(
            "INSERT INTO admin_audit(user_id, user_email, action, target, detail, ip) VALUES (?,?,?,?,?,?)",
            (uid, email, "register", "self", "first user → super_admin", request.remote_addr),
        )
    return resp


@bp.route("/api/auth/login", methods=["POST"])
def login():
    ip = request.remote_addr or "?"
    if auth.login_rate_limited(ip):
        return jsonify({"success": False, "error": "too many failed attempts; try again later"}), 429

    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    pw = data.get("password") or ""
    user = auth.authenticate(email, pw)
    auth.record_login_attempt(ip, email, success=bool(user))
    if not user:
        return jsonify({"success": False, "error": "invalid credentials"}), 401

    token = auth.issue_session(user["id"], ip, request.headers.get("User-Agent", ""))
    resp = jsonify({"success": True, "user": user})
    _set_session_cookie(resp, token)
    return resp


@bp.route("/api/auth/logout", methods=["POST"])
def logout():
    token = request.cookies.get(auth.SESSION_COOKIE)
    if token:
        auth.revoke_session(token)
    resp = jsonify({"success": True})
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


def _set_session_cookie(resp, token: str) -> None:
    # 12h session. Secure flag is set when behind TLS — we set it always since
    # we serve HTTPS in this deployment.
    resp.set_cookie(
        auth.SESSION_COOKIE,
        token,
        max_age=auth.SESSION_TTL_HOURS * 3600,
        httponly=True,
        secure=True,
        samesite="Strict",
        path="/",
    )


# ============================================================
# User management (super-admin only)
# ============================================================

@bp.route("/api/admin/users", methods=["GET"])
@auth.super_admin_required
def list_users():
    with get_conn() as c:
        rows = c.execute(
            """SELECT u.id, u.email, u.role, u.created_at, u.last_login_at,
                      cb.email AS created_by_email
               FROM users u LEFT JOIN users cb ON cb.id = u.created_by
               ORDER BY u.id"""
        ).fetchall()
    return jsonify({"success": True, "data": [dict(r) for r in rows]})


@bp.route("/api/admin/users", methods=["POST"])
@auth.super_admin_required
def add_user():
    data = request.get_json(silent=True) or {}
    email = (data.get("email") or "").strip()
    pw = data.get("password") or ""
    if not EMAIL_RE.match(email):
        return jsonify({"success": False, "error": "invalid email"}), 400
    if len(pw) < 12:
        return jsonify({"success": False, "error": "password must be at least 12 characters"}), 400
    try:
        uid = auth.create_user(email, pw, role="admin", created_by=request.user["id"])
    except Exception as exc:
        return jsonify({"success": False, "error": str(exc)}), 400
    auth.audit("create_user", target=email, detail=f"id={uid}")
    return jsonify({"success": True, "id": uid, "email": email})


@bp.route("/api/admin/users/<int:uid>", methods=["DELETE"])
@auth.super_admin_required
def remove_user(uid: int):
    if uid == request.user["id"]:
        return jsonify({"success": False, "error": "cannot delete yourself"}), 400
    with get_conn() as c:
        row = c.execute("SELECT email, role FROM users WHERE id = ?", (uid,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "not found"}), 404
        if row["role"] == "super_admin":
            return jsonify({"success": False, "error": "cannot delete super_admin"}), 400
        c.execute("DELETE FROM users WHERE id = ?", (uid,))
    auth.audit("delete_user", target=row["email"], detail=f"id={uid}")
    return jsonify({"success": True})


# ============================================================
# Settings (encrypted at rest)
# ============================================================

@bp.route("/api/admin/settings/schema", methods=["GET"])
@auth.login_required
def settings_schema():
    return jsonify({
        "success": True,
        "data": {
            "secret_keys": sorted(SECRET_SETTINGS),
            "public_keys": sorted(PUBLIC_SETTINGS),
        },
    })


@bp.route("/api/admin/settings", methods=["GET"])
@auth.login_required
def settings_get():
    """Return current settings.

    Secret values are NEVER returned in clear — we return whether the key is set
    and a masked preview (last 4 chars). Plain values (host, port, etc.) come
    back in full.
    """
    with get_conn() as c:
        rows = c.execute("SELECT key, value_enc, is_secret FROM settings").fetchall()

    out = {}
    for r in rows:
        try:
            plain = secrets_store.decrypt(r["value_enc"])
        except Exception:
            plain = ""
        if r["is_secret"]:
            out[r["key"]] = {
                "set": bool(plain),
                "preview": ("…" + plain[-4:]) if len(plain) >= 4 else ("…" if plain else ""),
            }
        else:
            out[r["key"]] = {"set": bool(plain), "value": plain}
    # Include keys that have no row yet, so the UI can render the form
    for k in ALL_SETTINGS:
        if k not in out:
            if k in SECRET_SETTINGS:
                out[k] = {"set": False, "preview": ""}
            else:
                out[k] = {"set": False, "value": ""}
    return jsonify({"success": True, "data": out})


@bp.route("/api/admin/settings", methods=["PUT"])
@auth.super_admin_required
def settings_put():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"success": False, "error": "expected object"}), 400

    updated = []
    with get_conn() as c:
        for key, value in data.items():
            if key not in ALL_SETTINGS:
                continue  # silently drop unknown keys
            if value is None:
                continue
            value_str = str(value)
            blob = secrets_store.encrypt(value_str)
            is_secret = 1 if key in SECRET_SETTINGS else 0
            c.execute(
                """INSERT INTO settings(key, value_enc, is_secret, updated_by)
                   VALUES (?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET
                       value_enc = excluded.value_enc,
                       is_secret = excluded.is_secret,
                       updated_at = datetime('now'),
                       updated_by = excluded.updated_by""",
                (key, blob, is_secret, request.user["id"]),
            )
            updated.append(key)
    auth.audit("update_settings", target=",".join(sorted(updated))[:200],
               detail=f"{len(updated)} keys")
    return jsonify({"success": True, "updated": updated})


# ============================================================
# CSV import — bulk-set settings from an org intake spreadsheet
# ============================================================

# CSV columns we recognize. Anything else in the file is ignored on purpose
# (warehouse address, billing address, ack flags, secondary/tertiary cards,
# etc.) — those have no slot in the current .env model.
CSV_COLUMN_MAP = {
    # csv_column         -> (template_key,                  normalizer_or_None)
    "good360_username":     ("GOOD360_{org}_EMAIL",          None),
    "good360_password":     ("GOOD360_{org}_PASSWORD",       None),
    "full_name":            ("CARD_{org}_NAME",              None),
    "primary_card_number":  ("CARD_{org}_NUMBER",            "digits"),
    "primary_exp_date":     ("CARD_{org}_EXPIRY",            "expiry"),
    "primary_cvv":          ("CARD_{org}_CVV",               "digits"),
    "primary_card_network": ("CARD_{org}_TYPE",              "card_type"),
}

CSV_TEMPLATE_HEADER = [
    "full_name", "organization_name",
    "good360_username", "good360_password",
    "primary_card_network", "primary_card_number",
    "primary_exp_date", "primary_cvv",
]

CSV_MAX_BYTES = 256 * 1024  # 256 KB — plenty for an org-intake sheet

_ORG_KEYWORDS = {
    "REVIVING_HOMES": ("reviving",),
    "HOPE4HUMANITY":  ("hope",),
}


def _detect_org(name: str) -> str | None:
    n = (name or "").strip().lower()
    if not n:
        return None
    for org, kws in _ORG_KEYWORDS.items():
        if any(k in n for k in kws):
            return org
    return None


def _normalize_expiry(raw: str) -> str:
    """Accept MM/YY, MM/YYYY, MM-YY, MMYY, MMYYYY, or with spaces. Return MMYY."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 4:
        return digits
    if len(digits) == 6:
        return digits[:2] + digits[4:]   # MMYYYY → MMYY
    if len(digits) == 5:
        # ambiguous — could be M-YYYY or MM-YYY, drop
        return ""
    return ""


def _normalize_card_type(raw: str) -> str:
    n = (raw or "").strip().lower().replace(" ", "")
    aliases = {
        "visa": "visa",
        "mastercard": "mastercard", "mc": "mastercard",
        "amex": "amex", "americanexpress": "amex", "ae": "amex",
        "discover": "discover",
    }
    return aliases.get(n, n)


def _digits_only(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


_NORMALIZERS = {
    "digits": _digits_only,
    "expiry": _normalize_expiry,
    "card_type": _normalize_card_type,
}


@bp.route("/api/admin/settings/csv-template", methods=["GET"])
@auth.login_required
def csv_template():
    """Download a CSV template with the columns we recognize and 2 sample rows."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_TEMPLATE_HEADER)
    w.writerow([
        "Jane Doe", "Reviving Homes",
        "jane@reviving.example", "changeme-12+chars",
        "Visa", "4111111111111111", "12/27", "123",
    ])
    w.writerow([
        "Bob Smith", "Hope 4 Humanity",
        "bob@hope4humanity.example", "changeme-12+chars",
        "Mastercard", "5555555555554444", "06/28", "456",
    ])
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=settings-template.csv"},
    )


@bp.route("/api/admin/settings/import-csv", methods=["POST"])
@auth.super_admin_required
def settings_import_csv():
    """Parse a CSV upload and upsert the recognized columns into encrypted settings.

    Does NOT write .env or restart services — operator must click Save & Apply
    afterward, so they can review the diff first.

    All exceptions are caught and returned as JSON so the frontend never gets a
    500 HTML page.
    """
    try:
        return _settings_import_csv_inner()
    except Exception as exc:  # noqa: BLE001 — broad on purpose
        import traceback
        traceback.print_exc()  # surface in container logs
        return jsonify({
            "success": False,
            "error": f"{type(exc).__name__}: {exc}",
        }), 500


def _settings_import_csv_inner():
    raw = _read_csv_payload()
    if isinstance(raw, tuple):  # (response, status)
        return raw

    # Try a few common encodings before giving up.
    text = None
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        return jsonify({"success": False, "error": "could not decode file (tried utf-8, cp1252, latin-1)"}), 400

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return jsonify({"success": False, "error": "CSV has no header row"}), 400

    field_set = {f.strip().lower() for f in reader.fieldnames if f}
    recognized = field_set & set(CSV_COLUMN_MAP.keys())
    ignored_columns = sorted(field_set - set(CSV_COLUMN_MAP.keys()) - {"organization_name"})

    rows_processed = 0
    rows_skipped: list[dict] = []
    updated_keys: set[str] = set()
    by_org: dict[str, int] = {}

    with get_conn() as c:
        for idx, row in enumerate(reader, start=2):  # row 1 is header
            rows_processed += 1
            row_l = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            org = _detect_org(row_l.get("organization_name", ""))
            if not org:
                rows_skipped.append({
                    "row": idx,
                    "reason": f"unknown organization_name: '{row_l.get('organization_name', '')}'",
                })
                continue
            by_org[org] = by_org.get(org, 0) + 1
            for csv_col, (key_tpl, norm_name) in CSV_COLUMN_MAP.items():
                if csv_col not in recognized:
                    continue
                value = row_l.get(csv_col, "")
                if not value:
                    continue
                if norm_name:
                    value = _NORMALIZERS[norm_name](value)
                    if not value:
                        rows_skipped.append({
                            "row": idx,
                            "reason": f"could not normalize '{csv_col}': '{row_l.get(csv_col)}'",
                        })
                        continue
                key = key_tpl.format(org=org)
                if key not in ALL_SETTINGS:
                    continue
                blob = secrets_store.encrypt(value)
                is_secret = 1 if key in SECRET_SETTINGS else 0
                c.execute(
                    """INSERT INTO settings(key, value_enc, is_secret, updated_by)
                       VALUES (?,?,?,?)
                       ON CONFLICT(key) DO UPDATE SET
                           value_enc = excluded.value_enc,
                           is_secret = excluded.is_secret,
                           updated_at = datetime('now'),
                           updated_by = excluded.updated_by""",
                    (key, blob, is_secret, request.user["id"]),
                )
                updated_keys.add(key)

    auth.audit(
        "import_csv",
        target=f"rows={rows_processed}",
        detail=f"updated={len(updated_keys)} skipped={len(rows_skipped)} orgs={','.join(sorted(by_org))}",
    )

    return jsonify({
        "success": True,
        "rows_processed": rows_processed,
        "rows_skipped": rows_skipped,
        "updated_keys": sorted(updated_keys),
        "ignored_columns": ignored_columns,
        "recognized_columns": sorted(recognized),
        "by_org": by_org,
    })


def _read_csv_payload() -> bytes | tuple:
    """Pull CSV bytes from either a multipart upload (`file` field) or a raw
    text/csv body. Enforces a size cap. Returns bytes on success, or a
    (jsonify-response, http-status) tuple on error."""
    if request.content_length and request.content_length > CSV_MAX_BYTES:
        return (jsonify({"success": False, "error": f"file too large (>{CSV_MAX_BYTES} bytes)"}), 413)
    f = request.files.get("file")
    if f:
        data = f.read(CSV_MAX_BYTES + 1)
        if len(data) > CSV_MAX_BYTES:
            return (jsonify({"success": False, "error": "file too large"}), 413)
        return data
    if request.data:
        if len(request.data) > CSV_MAX_BYTES:
            return (jsonify({"success": False, "error": "file too large"}), 413)
        return request.data
    return (jsonify({"success": False, "error": "no CSV payload (use multipart 'file' or raw body)"}), 400)


@bp.route("/api/admin/settings/apply", methods=["POST"])
@auth.super_admin_required
def settings_apply():
    """Write all current settings to .env and restart services.

    Restart is best-effort. If the docker socket isn't mounted in this
    container, we fall back to writing the .env file and reporting that the
    operator must restart manually.
    """
    written = _write_env_file()
    restarted = _restart_compose_services()
    auth.audit("apply_settings", target="env+restart",
               detail=f"wrote={len(written)} restart={restarted['ok']}")
    return jsonify({
        "success": True,
        "wrote_keys": written,
        "restart": restarted,
    })


def _write_env_file() -> list[str]:
    """Merge encrypted settings into ENV_FILE, preserving comments and key order
    where possible. Atomic via temp + rename."""
    if not ENV_FILE.exists():
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text("# Generated by mission control admin dashboard\n", encoding="utf-8")

    # Load encrypted settings → plain dict
    plain: dict[str, str] = {}
    with get_conn() as c:
        for r in c.execute("SELECT key, value_enc FROM settings").fetchall():
            try:
                plain[r["key"]] = secrets_store.decrypt(r["value_enc"])
            except Exception:
                continue

    existing = ENV_FILE.read_text(encoding="utf-8").splitlines()
    out_lines = []
    seen = set()
    for line in existing:
        m = re.match(r"^(?P<k>[A-Z_][A-Z0-9_]*)\s*=", line)
        if m and m.group("k") in plain:
            k = m.group("k")
            out_lines.append(f"{k}={plain[k]}")
            seen.add(k)
        else:
            out_lines.append(line)
    # Append any keys that weren't already present
    new_keys = sorted(k for k in plain if k not in seen)
    if new_keys:
        out_lines.append("")
        out_lines.append("# Added by admin dashboard at " + datetime.now(UTC).isoformat())
        for k in new_keys:
            out_lines.append(f"{k}={plain[k]}")

    tmp = ENV_FILE.with_suffix(".env.tmp")
    tmp.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(ENV_FILE)
    return sorted(plain.keys())


def _restart_compose_services() -> dict:
    """Restart the application services so they pick up the new .env.

    Skips `missioncontrol` itself (we'd kill ourselves). Requires the docker
    socket to be mounted at /var/run/docker.sock and `docker` CLI present.
    """
    if not Path("/var/run/docker.sock").exists() or not shutil.which("docker"):
        return {"ok": False, "reason": "docker socket / cli not available; restart manually"}

    services = ["monitor", "daemon", "watchdog", "telegram-bot", "intake"]
    cmd = ["docker", "compose", "-f", "/app/docker-compose.yml", "restart", *services]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd="/app")
        return {
            "ok": proc.returncode == 0,
            "rc": proc.returncode,
            "stdout": proc.stdout[-1000:],
            "stderr": proc.stderr[-1000:],
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "reason": "restart timed out"}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


# ============================================================
# Scans, purchases, audit (read-only views)
# ============================================================

@bp.route("/api/admin/scans", methods=["GET"])
@auth.login_required
def admin_scans():
    """Recent scan activity. Reads heartbeat + run log + cron log."""
    limit = request.args.get("limit", 100, type=int)
    heartbeat = _safe_json(HEARTBEAT)
    run_log = _safe_json(RUN_LOG)
    runs = run_log.get("runs", run_log) if isinstance(run_log, dict) else (run_log or [])

    scans = []
    for entry in runs[-limit:]:
        scans.append({
            "time": entry.get("time") or entry.get("detected_at"),
            "org_id": entry.get("org_id"),
            "title": entry.get("title") or entry.get("truck_title"),
            "url": entry.get("url"),
            "price": entry.get("price"),
            "status": entry.get("status", "scanned"),
            "login_ok": entry.get("login_ok"),
        })

    # Service status — true source of truth for "is the scanner alive".
    services = _docker_service_states([
        "monitor", "daemon", "watchdog", "telegram-bot", "missioncontrol", "intake",
    ])

    return jsonify({
        "success": True,
        "data": {
            "scans": scans,
            "heartbeat": heartbeat,
            "services": services,
        },
    })


@bp.route("/api/admin/scans/log-tail", methods=["GET"])
@auth.login_required
def scans_log_tail():
    """Recent monitor lines, classified by severity. Reads from `docker
    compose logs monitor` so we see whatever the script is printing (stdout)
    rather than depending on the script writing to a specific file."""
    n = max(1, min(request.args.get("n", 200, type=int), 1000))
    service = request.args.get("service", "monitor")

    raw_lines = _docker_logs(service, n)
    if not raw_lines:
        # Fallback to file if docker logs unavailable for some reason.
        raw_lines = _read_last_lines(CRON_LOG, n)

    classified = []
    for raw in raw_lines:
        # Strip the "monitor-1  | " prefix docker-compose adds.
        line = raw
        if " | " in line[:40]:
            line = line.split(" | ", 1)[1]
        sev = "info"
        upper = line.upper()
        if "ERROR" in upper or "FAIL" in upper or "TRACEBACK" in upper or "❌" in line:
            sev = "error"
        elif "WARN" in upper or "MISSED" in upper or "⚠" in line:
            sev = "warn"
        elif "✅" in line or " SUCCESS" in upper or "PURCHASED" in upper or "[AUTO-BUY: ACTIVE" in line:
            sev = "ok"
        elif "[EXCLUDED]" in line or "[TRACKED]" in line or "[skipped]" in line:
            sev = "info"
        classified.append({"line": line, "severity": sev})

    return jsonify({
        "success": True,
        "data": {
            "lines": classified,
            "error_count": sum(1 for l in classified if l["severity"] == "error"),
            "warn_count":  sum(1 for l in classified if l["severity"] == "warn"),
            "ok_count":    sum(1 for l in classified if l["severity"] == "ok"),
            "total": len(classified),
            "service": service,
            "source": "docker logs" if raw_lines else "no source",
        },
    })


COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "good-360-bids")


def _docker_logs(service: str, n: int) -> list[str]:
    """Read the last `n` log lines from a compose service via the docker CLI."""
    if not shutil.which("docker"):
        return []
    try:
        proc = subprocess.run(
            ["docker", "compose", "-p", COMPOSE_PROJECT,
             "-f", "/app/docker-compose.yml", "logs",
             "--no-color", "--tail", str(n), service],
            capture_output=True, text=True, timeout=10, cwd="/app",
        )
        if proc.returncode != 0:
            return []
        return [ln for ln in proc.stdout.splitlines() if ln.strip()]
    except Exception:
        return []


def _read_last_lines(path: str, n: int) -> list[str]:
    if not os.path.exists(path):
        return []
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            block = 65536
            data = b""
            while size > 0 and data.count(b"\n") <= n:
                step = min(block, size)
                size -= step
                f.seek(size)
                data = f.read(step) + data
        text = data.decode("utf-8", errors="replace")
        return [ln for ln in text.splitlines()[-n:] if ln.strip()]
    except OSError:
        return []


def _docker_service_states(names: list[str]) -> list[dict]:
    """Inspect docker for the given compose services. Best-effort.

    Treats `restarting` as `running` for services like `monitor` whose normal
    operating mode is run-once-then-exit (compose's `restart: always` brings
    it back). The user sees "running" for healthy services and the log tail
    lets them spot a crash-loop separately.
    """
    if not shutil.which("docker"):
        return [{"name": n, "state": "unknown", "running": None} for n in names]
    try:
        proc = subprocess.run(
            ["docker", "compose", "-p", COMPOSE_PROJECT,
             "-f", "/app/docker-compose.yml", "ps", "-a", "--format", "json"],
            capture_output=True, text=True, timeout=10, cwd="/app",
        )
        if proc.returncode != 0:
            return [{"name": n, "state": "unknown", "running": None} for n in names]
        # Each service is a separate JSON object on its own line, OR a JSON array
        # depending on compose version. Handle both.
        out = proc.stdout.strip()
        services = []
        if out.startswith("["):
            services = json.loads(out)
        else:
            for line in out.splitlines():
                if line.strip():
                    try:
                        services.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        by_name = {s.get("Service", s.get("Name", "")).split("-")[-1].rstrip("0123456789"): s for s in services}
        # The above split is brittle; fall back to substring match.
        result = []
        for n in names:
            match = None
            for s in services:
                svc_field = (s.get("Service") or "").lower()
                name_field = (s.get("Name") or "").lower()
                if svc_field == n.lower() or n.lower() in name_field:
                    match = s
                    break
            if match:
                state = (match.get("State") or "").lower()
                health = (match.get("Health") or "").lower()
                # `restarting` is normal for run-once-then-exit scripts under
                # restart: always — treat as running. The log tail is the
                # crash-loop detector if it ever degenerates to one.
                running = state in ("running", "restarting")
                result.append({
                    "name": n,
                    "state": state,
                    "running": running,
                    "health": health or None,
                })
            else:
                result.append({"name": n, "state": "stopped", "running": False, "health": None})
        return result
    except Exception:
        return [{"name": n, "state": "unknown", "running": None} for n in names]


@bp.route("/api/admin/purchases", methods=["GET"])
@auth.login_required
def admin_purchases():
    """Purchase attempts: pass + fail. Reads append-only audit JSONL files."""
    limit = request.args.get("limit", 200, type=int)
    days = request.args.get("days", 14, type=int)
    entries = _read_audit(days_back=days)
    purchases = [e for e in entries if e.get("event", "").startswith("purchase")]
    purchases.sort(key=lambda e: e.get("ts", ""), reverse=True)
    return jsonify({"success": True, "data": purchases[:limit]})


@bp.route("/api/admin/audit", methods=["GET"])
@auth.super_admin_required
def admin_audit_log():
    """Dashboard's own admin-action audit (separate from purchase audit)."""
    limit = request.args.get("limit", 200, type=int)
    with get_conn() as c:
        rows = c.execute(
            "SELECT * FROM admin_audit ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return jsonify({"success": True, "data": [dict(r) for r in rows]})


def _safe_json(path: str):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return None


def _read_audit(days_back: int = 14) -> list[dict]:
    if not AUDIT_DIR.exists():
        return []
    cutoff = time.time() - (days_back * 86400)
    entries = []
    for p in sorted(AUDIT_DIR.glob("audit-*.jsonl")):
        if p.stat().st_mtime < cutoff:
            continue
        try:
            with p.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except OSError:
            continue
    return entries


# ============================================================
# QuickBeed customer sync — webhook receiver, list view, manual sync
# ============================================================

@bp.route("/api/webhooks/quickbeed", methods=["POST"])
def quickbeed_webhook():
    """Receive a QuickBeed customer-changed event.

    Verifies HMAC-SHA256 of the raw body using QUICKBEED_WEBHOOK_SECRET,
    dedups on X-Event-Id, and returns 2xx within 10s. Record refresh runs in
    a background thread so the response stays fast (the contract gives us 10s).
    """
    raw = request.get_data(cache=False, as_text=False) or b""
    sig = request.headers.get("X-Signature", "")
    event_id = request.headers.get("X-Event-Id", "")

    secret = quickbeed._setting("QUICKBEED_WEBHOOK_SECRET")
    if not secret:
        return jsonify({"error": "webhook secret not configured"}), 503

    expected = "hmac-sha256=" + hmac.new(
        secret.encode("utf-8"), raw, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        return jsonify({"error": "invalid signature"}), 401
    if not event_id:
        return jsonify({"error": "missing X-Event-Id"}), 400

    try:
        payload = json.loads(raw.decode("utf-8") or "{}")
    except json.JSONDecodeError:
        return jsonify({"error": "invalid json"}), 400

    body_sha = hashlib.sha256(raw).hexdigest()

    # Dedup. INSERT OR IGNORE keeps the first delivery as authoritative.
    with get_conn() as c:
        cur = c.execute(
            """INSERT OR IGNORE INTO webhook_events
                  (event_id, event, customer_id, body_sha256)
                VALUES (?,?,?,?)""",
            (event_id, payload.get("event"), payload.get("customer_id"), body_sha),
        )
        new_event = cur.rowcount > 0

    if not new_event:
        # Already processed (or in flight). Acknowledge and move on.
        return jsonify({"success": True, "deduped": True}), 200

    # Fast path: status-only events update the local row inline. Anything
    # heavier hands off to a background thread so we still return inside 10s.
    event = payload.get("event")
    cid = payload.get("customer_id")

    try:
        if event == "customer.status_changed" and cid:
            quickbeed.update_status(
                cid,
                status=payload.get("current_status") or "unknown",
                updated_at=payload.get("occurred_at"),
            )
        # Always queue a background refresh so the local mirror picks up other
        # field changes the webhook payload doesn't enumerate.
        threading.Thread(
            target=_quickbeed_refresh_one,
            args=(cid, event_id),
            daemon=True,
        ).start()
    except Exception:
        # Webhook ack still goes out — the dispatcher would retry on a 500
        # anyway, but our processing failure shouldn't block ack since dedup
        # would then prevent reprocessing. Log + continue.
        import traceback
        traceback.print_exc()

    return jsonify({"success": True}), 200


def _quickbeed_refresh_one(customer_id: str, event_id: str) -> None:
    """Pull the full record (no creds — uses reconciliation reason) and mark
    the webhook event processed. Runs in a daemon thread."""
    if not customer_id:
        return
    try:
        client = quickbeed.QuickBeedClient.from_settings()
        # Use status endpoint first to pick up status-only changes cheaply.
        # Then upsert from the list filtered to this id (avoids `?reason=` cost).
        live = client.get_status(customer_id)
        quickbeed.update_status(
            customer_id, status=live.get("status"),
            updated_at=live.get("updated_at"),
        )
        # Pull full record (without creds) to refresh profile/ops fields.
        # Use 'reconciliation' reason so the audit log reflects sync intent.
        rec = client.get_customer(customer_id, reason=quickbeed.REASON_RECONCILIATION)
        quickbeed.upsert_customer(rec)
    except Exception:
        import traceback
        traceback.print_exc()
    finally:
        with get_conn() as c:
            c.execute(
                "UPDATE webhook_events SET processed_at = datetime('now') WHERE event_id = ?",
                (event_id,),
            )


@bp.route("/api/admin/customers", methods=["GET"])
@auth.login_required
def list_customers_local():
    """List the local customer mirror — what the round-robin sees."""
    status = request.args.get("status")
    q = "SELECT * FROM customers"
    args: list = []
    if status:
        q += " WHERE status = ?"
        args.append(status)
    q += " ORDER BY organization_name COLLATE NOCASE"
    with get_conn() as c:
        rows = c.execute(q, args).fetchall()
    customers = [dict(r) for r in rows]
    # Aggregate counts for the metric strip
    summary = {"total": len(customers)}
    for r in customers:
        summary[r["status"]] = summary.get(r["status"], 0) + 1
    return jsonify({"success": True, "data": customers, "summary": summary})


@bp.route("/api/admin/customers/<customer_id>", methods=["GET"])
@auth.login_required
def customer_detail(customer_id):
    """Return everything we have locally for a single customer. No live API
    call — fast, safe to call repeatedly. Sensitive fields are not stored
    locally so they're not in the response either."""
    with get_conn() as c:
        row = c.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if not row:
        return jsonify({"success": False, "error": "not found"}), 404
    return jsonify({"success": True, "data": dict(row)})


@bp.route("/api/admin/customers/<customer_id>/live", methods=["GET"])
@auth.super_admin_required
def customer_detail_live(customer_id):
    """Fetch the live record from QuickBeed, return SAFE projection.

    Card numbers and CVVs are reduced to last-4 / masked length; the password
    is reduced to its length only. The username is shown in full because it's
    visible at login screens anyway and is needed to identify accounts.

    The upstream call uses ?reason=support_investigation, which QuickBeed
    audit-logs against this dashboard's consumer identity. Effectively: every
    'Show full record' click is recorded on QuickBeed's side too.
    """
    reason = request.args.get("reason") or quickbeed.REASON_SUPPORT
    try:
        rec = quickbeed.fetch_full(customer_id, reason=reason)
    except quickbeed.QuickBeedHTTPError as exc:
        return jsonify({"success": False, "error": str(exc), "status": exc.status}), 502
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    pc = rec.get("partner_credentials") or {}
    safe_pc = {
        "username": pc.get("username"),
        "password_length": len(pc.get("password") or ""),
        "password_present": bool(pc.get("password")),
    }
    safe_cards = []
    for pm in (rec.get("payment_methods") or []):
        cn = pm.get("card_number") or ""
        cvv = pm.get("cvv") or ""
        safe_cards.append({
            "rank": pm.get("rank"),
            "type": pm.get("type"),
            "card_network": pm.get("card_network"),
            "name_on_card": pm.get("name_on_card"),
            "card_last4": cn[-4:] if cn else None,
            "card_present": bool(cn),
            "cvv_length": len(cvv),
            "cvv_present": bool(cvv),
            "exp_month": pm.get("exp_month"),
            "exp_year": pm.get("exp_year"),
            "billing_address": pm.get("billing_address"),
        })

    auth.audit("customer_live_view", target=customer_id, detail=f"reason={reason}")

    return jsonify({
        "success": True,
        "data": {
            "id": rec.get("id"),
            "status": rec.get("status"),
            "status_reason": rec.get("status_reason"),
            "created_at": rec.get("created_at"),
            "updated_at": rec.get("updated_at"),
            "profile": rec.get("profile"),
            "operations": rec.get("operations"),
            "acknowledgements": rec.get("acknowledgements"),
            "partner_credentials": safe_pc,
            "payment_methods": safe_cards,
            "_reason_logged": reason,
        },
    })


@bp.route("/api/admin/customers/sync", methods=["POST"])
@auth.super_admin_required
def trigger_sync():
    """Manual resync. Pass ?bootstrap=1 for a full pull, otherwise incremental."""
    full = request.args.get("bootstrap") == "1"
    try:
        if full:
            result = quickbeed.bootstrap()
        else:
            result = quickbeed.incremental_sync()
        auth.audit("quickbeed_sync",
                   target="bootstrap" if full else "incremental",
                   detail=json.dumps(result))
        return jsonify({"success": True, "mode": "bootstrap" if full else "incremental",
                        "result": result})
    except quickbeed.QuickBeedConfigError as exc:
        return jsonify({"success": False, "error": f"config: {exc}"}), 400
    except quickbeed.QuickBeedHTTPError as exc:
        return jsonify({"success": False, "error": str(exc), "status": exc.status}), 502
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500


@bp.route("/api/admin/customers/test-connection", methods=["POST"])
@auth.login_required
def test_connection():
    """Hit /health and a one-page list to confirm the token + IP allowlist work."""
    try:
        client = quickbeed.QuickBeedClient.from_settings()
        h = client.health()
        # Try a single list call too — that's what actually exercises auth.
        page1, pag = client.list_customers(page=1, page_size=1, reason=quickbeed.REASON_RECONCILIATION)
        return jsonify({
            "success": True,
            "health": h,
            "auth_ok": True,
            "first_record_id": (page1[0]["id"] if page1 else None),
            "total_customers": pag.get("total"),
        })
    except quickbeed.QuickBeedConfigError as exc:
        return jsonify({"success": False, "error": f"config: {exc}"}), 400
    except quickbeed.QuickBeedHTTPError as exc:
        return jsonify({"success": False, "error": str(exc), "status": exc.status}), 502
    except Exception as exc:  # noqa: BLE001
        return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500


# ============================================================
# Internal API for the orchestrator (server-to-server, X-API-Key auth)
# Used by autobuy_v2 to fetch a full org_config dict on demand.
# ============================================================

@bp.route("/api/internal/org-config/<customer_id>", methods=["GET"])
def internal_org_config(customer_id):
    """Return an org_config dict (creds + cards) for a QuickBeed customer.

    Auth: X-API-Key header must match MISSIONCONTROL_API_KEY env var.
    The required ?reason= is forwarded to the QuickBeed audit log.
    """
    expected_key = os.environ.get("MISSIONCONTROL_API_KEY", "")
    if not expected_key or request.headers.get("X-API-Key") != expected_key:
        return jsonify({"error": "unauthorized"}), 401

    reason = request.args.get("reason") or quickbeed.REASON_CREDENTIAL_USE

    try:
        rec = quickbeed.fetch_full(customer_id, reason=reason)
    except quickbeed.QuickBeedHTTPError as exc:
        return jsonify({"error": str(exc), "status": exc.status}), 502
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return jsonify({"error": f"{type(exc).__name__}: {exc}"}), 500

    # Translate QuickBeed shape → org_config dict that autobuy_v2 understands.
    # Keep the QuickBeed UUID and explicit dry-run flag visible to the caller.
    profile = rec.get("profile") or {}
    pc = rec.get("partner_credentials") or {}
    ops = rec.get("operations") or {}
    cards = rec.get("payment_methods") or []

    primary = next((c for c in cards if c.get("rank") == "primary"), None) or (cards[0] if cards else None)

    org_config = {
        "quickbeed_customer_id": rec.get("id"),
        "name": profile.get("organization_name"),
        "good360_email": pc.get("username"),
        "good360_password": pc.get("password"),
        "warehouse_address": ops.get("warehouse_address"),
        "has_dock": bool(ops.get("has_loading_dock")),
        "max_price": ops.get("max_budget"),
        "auto_buy_targets": [s.strip() for s in (ops.get("truck_selection") or "").split(",") if s.strip()],
        "card": _card_to_org(primary) if primary else None,
        "fallback_cards": [_card_to_org(c) for c in cards if c is not primary],
        "checkout_answers": {
            "people_helped": str(ops.get("people_served") or ""),
            "distribution_method": ops.get("distribution_method") or "",
            "warehouse_address": ops.get("warehouse_address") or "",
            "dock_pallet": "Yes, we have a dock" if ops.get("has_loading_dock") else "No dock",
        },
        "email_alerts": [profile.get("email")] if profile.get("email") else [],
    }
    return jsonify({"success": True, "org_config": org_config, "status": rec.get("status")})


def _card_to_org(pm: dict | None) -> dict | None:
    if not pm:
        return None
    em = pm.get("exp_month")
    ey = pm.get("exp_year")
    expiry_mmyy = ""
    try:
        if em is not None and ey is not None:
            expiry_mmyy = f"{int(em):02d}{int(ey) % 100:02d}"
    except (TypeError, ValueError):
        expiry_mmyy = ""
    return {
        "name": pm.get("name_on_card"),
        "number": pm.get("card_number"),
        "expiry": expiry_mmyy,
        "cvv": pm.get("cvv"),
        "type": pm.get("card_network"),
        "rank": pm.get("rank"),
        "billing": pm.get("billing_address"),
    }


def register_admin(app):
    """Mount this blueprint onto an existing Flask app."""
    app.register_blueprint(bp)
