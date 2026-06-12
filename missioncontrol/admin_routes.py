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
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from pathlib import Path

import csv
import hashlib
import hmac
import io
import threading
import urllib.error
import urllib.request

from flask import Blueprint, Response, jsonify, redirect, request, send_from_directory

import auth
import customer_readiness
from db import count_users, get_conn, has_any_user
import quickbeed
import secrets_store

# Live View proxies HTTP calls to the persistent-browser daemon. Compose's
# embedded DNS resolves `daemon` to that container's IP on the bridge
# network; port 5002 matches good360_daemon.py's DAEMON_PORT.
DAEMON_BASE_URL = os.environ.get("DAEMON_URL", "http://daemon:5002")

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
    # OpenRouter — gateway used for the AI failure-diagnosis feature.
    # Claude Haiku is reached through OpenRouter so the operator can
    # swap models / providers without re-deploying the dashboard.
    "OPENROUTER_API_KEY",
    # QuickBeed customer-sync (the secret bits)
    "QUICKBEED_API_TOKEN", "QUICKBEED_WEBHOOK_SECRET",
    # Sandbox-mode test creds + card. Treated as secrets so they share the same
    # at-rest encryption + masking as live values, even though the data is fake.
    "SANDBOX_GOOD360_PASSWORD",
    "SANDBOX_CARD_NUMBER", "SANDBOX_CARD_EXPIRY", "SANDBOX_CARD_CVV",
}
PUBLIC_SETTINGS = {
    "CARD_REVIVING_HOMES_TYPE", "CARD_HOPE4HUMANITY_TYPE",
    "TELEGRAM_GROUP_REVIVING_HOMES", "TELEGRAM_GROUP_HOPE4HUMANITY", "TELEGRAM_OPERATOR_CHAT_ID",
    "SMTP_HOST", "SMTP_PORT", "SMTP_USER", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO",
    "TZ", "LOG_LEVEL", "WORKDIR",
    # Scan loop cadence (seconds between scan cycles). Surfaced as a slider
    # in the dashboard so the operator can dial detection latency vs
    # request-volume to Good360.
    "MONITOR_INTERVAL_SECONDS",
    "AUTOBUY_ENGINE", "DEVTOOLS_AGENT_MODEL", "DEVTOOLS_AGENT_DRY_RUN",
    "DEVTOOLS_AGENT_TIMEOUT_SECONDS", "DEVTOOLS_AGENT_ISOLATED",
    "DEVTOOLS_AGENT_FALLBACK_ON_FAILED", "DEVTOOLS_CHROME_EXECUTABLE",
    "DEVTOOLS_AGENT_ALLOW_SECRETS_TO_MODEL", "DEVTOOLS_AGENT_ALLOW_LIVE_PURCHASE",
    # QuickBeed customer-sync (non-secret bits)
    "QUICKBEED_BASE_URL", "QUICKBEED_APP_ID", "QUICKBEED_CONSUMER_ID",
    "QUICKBEED_POLL_INTERVAL_SECONDS", "QUICKBEED_DRY_RUN",
    # Sandbox mode — when true the scan/autobuy stack swaps to the test site.
    "SANDBOX_MODE",
    "SANDBOX_GOOD360_BASE_URL", "SANDBOX_GOOD360_EMAIL",
    "SANDBOX_CARD_NAME", "SANDBOX_CARD_TYPE",
    # OpenRouter model id used by the failure-diagnosis feature. Operator
    # can switch (e.g. to anthropic/claude-sonnet-4.6 for richer output,
    # or anthropic/claude-3.5-haiku for cost) without code changes.
    "OPENROUTER_MODEL",
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
        "sandbox_mode": _sandbox_mode_active(),
    })


def _sandbox_mode_active() -> bool:
    """True if the sandbox toggle is on in the encrypted settings store.

    Read from the DB rather than os.environ because the dashboard process is
    long-lived; SANDBOX_MODE in our own env is whatever was set when the
    container started, which lags behind toggles until the next Apply. The DB
    value is the source of truth the operator just clicked.
    """
    try:
        with get_conn() as c:
            row = c.execute(
                "SELECT value_enc FROM settings WHERE key = 'SANDBOX_MODE'"
            ).fetchone()
        if not row:
            return False
        v = secrets_store.decrypt(row["value_enc"]) or ""
        return v.strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return False


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

    # Load encrypted settings → plain dict. Skip keys whose decrypted value is
    # empty/whitespace: docker-compose's env_file beats the Dockerfile's `ENV`
    # directives, so writing `WORKDIR=` (empty) into .env clobbers the
    # `ENV WORKDIR=/app/workdir` baked into the image and breaks the
    # healthcheck (Path("") + "good360_heartbeat.json" → wrong location).
    # Treat "no value in the dashboard" as "leave the Dockerfile default".
    plain: dict[str, str] = {}
    with get_conn() as c:
        for r in c.execute("SELECT key, value_enc FROM settings").fetchall():
            try:
                v = secrets_store.decrypt(r["value_enc"])
            except Exception:
                continue
            if v is None or str(v).strip() == "":
                continue
            plain[r["key"]] = v

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

    # Write in place. We can't use the usual temp-file + atomic rename pattern
    # because docker-compose mounts this .env as a single-file bind mount —
    # rename() over a bind-mounted file fails with EBUSY (the kernel won't
    # swap the inode the bind mount is pinned to). The write is small and
    # only triggered by an explicit operator Apply, so the brief
    # non-atomic window is acceptable.
    ENV_FILE.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    os.chmod(ENV_FILE, 0o600)
    return sorted(plain.keys())


def _restart_compose_services() -> dict:
    """Restart the application services so they pick up the new .env.

    Skips `missioncontrol` itself (we'd kill ourselves). Requires the docker
    socket to be mounted at /var/run/docker.sock and `docker` CLI present.
    """
    if not Path("/var/run/docker.sock").exists() or not shutil.which("docker"):
        return {"ok": False, "reason": "docker socket / cli not available; restart manually"}

    services = ["monitor", "daemon", "watchdog", "telegram-bot", "intake"]
    # `up -d` (not `restart`) — `restart` reuses the existing container, which
    # was created with the OLD env_file. We need compose to recreate the
    # containers so the freshly-written .env is read at container creation.
    #
    # --project-directory is the HOST path. Relative bind mounts in
    # docker-compose.yml (e.g. ./settings_bootstrap.py) get resolved against
    # the project directory, and the resulting paths are sent to the docker
    # daemon — which resolves them on the host. Without this, paths resolve
    # against /app inside this container and the daemon then fails to mount
    # them ("not a directory"). The same host path is bind-mounted into this
    # container (read-only) so the compose CLI can also read the project .env.
    project_dir = os.environ.get("DASHBOARD_PROJECT_DIR", "/root/good-360-bids")
    cmd = [
        "docker", "compose",
        "--project-directory", project_dir,
        "-f", "/app/docker-compose.yml",
        "up", "-d", *services,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=project_dir)
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


@bp.route("/api/admin/restart", methods=["POST"])
@auth.super_admin_required
def restart_stack():
    """Full-stack restart triggered from the dashboard's status pill.

    Sibling services (monitor/daemon/watchdog/telegram-bot/intake) are cycled
    synchronously via `docker compose up -d` so config changes (e.g. init:true)
    propagate. Missioncontrol then restarts itself via a detached `docker
    restart $HOSTNAME` after a short delay — the sleep lets this HTTP
    response flush before the container dies. `docker restart` is a single
    daemon API call, so once issued it completes regardless of the caller.
    """
    if not Path("/var/run/docker.sock").exists() or not shutil.which("docker"):
        return jsonify({"ok": False, "reason": "docker socket / cli not available"}), 503

    siblings = _restart_compose_services()

    self_container = os.environ.get("HOSTNAME", "")
    if self_container:
        subprocess.Popen(
            ["sh", "-c", f"sleep 3 && docker restart {self_container}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    auth.audit("restart_stack", target=self_container or "missioncontrol",
               detail=f"siblings_ok={siblings.get('ok')}")
    return jsonify({
        "ok": True,
        "siblings": siblings,
        "self_restart_in_seconds": 3,
    })


# ============================================================
# Live View — operator-driven remote browser
# ============================================================
# Thin proxy in front of good360_daemon.py's /live/* endpoints, plus a
# customer (org) list so the dashboard can render a pick-list. Auth-gated
# at super_admin level since these can drive a real (live) checkout.

def _daemon_request(method: str, path: str, payload: dict | None = None, timeout: int = 60):
    """Tiny urllib wrapper. Returns (status_code, body_bytes, headers_dict).
    Network errors collapse into a 502-shaped tuple so the caller doesn't
    have to branch on exception types."""
    url = DAEMON_BASE_URL.rstrip("/") + path
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read(), dict(resp.getheaders())
    except urllib.error.HTTPError as e:
        return e.code, e.read() if hasattr(e, "read") else b"", dict(e.headers or {})
    except urllib.error.URLError as e:
        return 502, json.dumps({"status": "error", "message": f"daemon unreachable: {e.reason}"}).encode(), {}
    except Exception as e:
        return 502, json.dumps({"status": "error", "message": str(e)}).encode(), {}


def _live_view_is_sandbox() -> bool:
    """Match sandbox.is_sandbox() without importing it (avoids the /app
    path-juggling needed for `config`)."""
    return (os.environ.get("SANDBOX_MODE", "") or "").strip().lower() in ("1", "true", "yes", "on")


def _live_master_card_number() -> str:
    """The operator's card on file for live checkouts. The legacy schema
    keeps card data in env vars rather than per-customer rows in QuickBeed,
    so every live customer pastes the same numbers — that's expected.
    Falls back gracefully if the Reviving Homes slot isn't configured."""
    for prefix in ("CARD_REVIVING_HOMES", "CARD_HOPE4HUMANITY"):
        n = (os.environ.get(f"{prefix}_NUMBER") or "").strip()
        if n:
            return prefix
    return "CARD_REVIVING_HOMES"


@bp.route("/api/admin/live/customers", methods=["GET"])
@auth.login_required
def live_customers():
    """List customers for the Live View picker.

    In **live mode** these are the real QuickBeed-synced nonprofits from
    the `customers` table (status=active). In **sandbox mode** we fall
    back to the two-org legacy config (load_orgs) so test runs stay self-
    contained and don't expose real customer data.
    """
    out = []
    if _live_view_is_sandbox():
        try:
            import sys as _sys
            if "/app" not in _sys.path:
                _sys.path.insert(0, "/app")
            import config as _cfg
            orgs = _cfg.load_orgs()
        except Exception as e:
            return jsonify({"success": False, "error": f"orgs load failed: {e}"}), 500
        for key, org in orgs.items():
            card = org.get("card") or {}
            card_number = card.get("number") or ""
            out.append({
                "key":            key,
                "name":           org.get("name") or key,
                "good360_email":  org.get("good360_email") or "",
                "card_last4":     card_number[-4:] if len(card_number) >= 4 else "",
                "card_brand":     card.get("type") or "",
                "max_auto_pay":   org.get("max_auto_pay") or org.get("max_price"),
                "auto_buy":       bool(org.get("auto_buy")),
                "paused":         bool(org.get("paused")),
                "source":         "sandbox-orgs",
            })
    else:
        # Live mode — real QuickBeed customers.
        prefix = _live_master_card_number()
        master_number = (os.environ.get(f"{prefix}_NUMBER") or "").strip()
        master_last4 = master_number[-4:] if len(master_number) >= 4 else ""
        master_brand = (os.environ.get(f"{prefix}_TYPE") or "").strip()
        with get_conn() as c:
            rows = c.execute(
                "SELECT id, organization_name, email, status, max_budget "
                "FROM customers WHERE status='active' "
                "ORDER BY organization_name COLLATE NOCASE"
            ).fetchall()
        for r in rows:
            out.append({
                "key":            r["id"],                  # QuickBeed UUID
                "name":           r["organization_name"] or "(unnamed)",
                "good360_email":  r["email"] or "",
                "card_last4":     master_last4,             # global card on file
                "card_brand":     master_brand,
                "max_auto_pay":   r["max_budget"],
                "auto_buy":       True,
                "paused":         False,
                "source":         "quickbeed",
            })

    out.sort(key=lambda o: o["name"].lower())
    return jsonify({"data": out, "count": len(out), "mode": "sandbox" if _live_view_is_sandbox() else "live"})


@bp.route("/api/admin/live/customer/<path:key>", methods=["GET"])
@auth.login_required
def live_customer_detail(key):
    """Full detail for one customer, shaped for the Live View copy-buttons.

    Live mode: pull the row from the QuickBeed `customers` table and graft
    on the operator's global card + scan creds. Sandbox mode: fall through
    to the legacy org dict from good360_orgs_master.example.json.
    """
    if _live_view_is_sandbox():
        try:
            import sys as _sys
            if "/app" not in _sys.path:
                _sys.path.insert(0, "/app")
            import config as _cfg
            orgs = _cfg.load_orgs()
        except Exception as e:
            return jsonify({"success": False, "error": f"orgs load failed: {e}"}), 500
        org = orgs.get(key)
        if not org:
            return jsonify({"success": False, "error": "not found"}), 404
        return jsonify({"data": org})

    # Live mode — real customer
    with get_conn() as c:
        row = c.execute("SELECT * FROM customers WHERE id = ?", (key,)).fetchone()
    if not row:
        return jsonify({"success": False, "error": "not found"}), 404
    d = dict(row)

    # Card data lives globally in env (legacy schema). Surface whichever
    # CARD_<ORG>_* slot is populated so the operator can paste through it.
    prefix = _live_master_card_number()
    master_card = {
        "name":   os.environ.get(f"{prefix}_NAME", ""),
        "number": os.environ.get(f"{prefix}_NUMBER", ""),
        "expiry": os.environ.get(f"{prefix}_EXPIRY", ""),
        "cvv":    os.environ.get(f"{prefix}_CVV", ""),
        "type":   os.environ.get(f"{prefix}_TYPE", ""),
    }

    # Good360 login is the operator's master scan account (one account
    # purchases on behalf of every nonprofit).
    good360_email    = os.environ.get("SCAN_GOOD360_EMAIL", "")
    good360_password = os.environ.get("SCAN_GOOD360_PASSWORD", "")

    return jsonify({"data": {
        "name":                d.get("organization_name") or key,
        "good360_email":       good360_email,
        "good360_password":    good360_password,
        "card":                master_card,
        # The customers table has one warehouse address — reuse it for
        # both shipping + billing display until a per-customer billing
        # field is added.
        "billing_address_line1": d.get("warehouse_address") or "",
        # Checkout questions map to the QuickBeed fields:
        "checkout_answers": {
            "people_helped":        str(d.get("people_served") or ""),
            "distribution_method":  d.get("distribution_method") or "",
        },
        # Extras the operator may want at hand:
        "contact_email":  d.get("email") or "",
        "contact_phone":  d.get("phone") or "",
        "max_budget":     d.get("max_budget"),
        "preferred_location": d.get("preferred_location") or "",
        "truck_selection":    d.get("truck_selection") or "",
    }})


@bp.route("/api/admin/live/screenshot", methods=["GET"])
@auth.login_required
def live_screenshot():
    """Stream the daemon's current Live View viewport as PNG.

    Pass a `_ts` cache-buster query — the frontend uses it to defeat
    browser caching during polling. We just forward to the daemon.
    """
    code, body, _hdrs = _daemon_request("GET", "/live/screenshot")
    if code != 200:
        # Daemon returns JSON on errors; pass that through.
        return Response(body, status=code, mimetype="application/json")
    return Response(body, mimetype="image/png", headers={"Cache-Control": "no-store"})


@bp.route("/api/admin/live/navigate", methods=["POST"])
@auth.super_admin_required
def live_navigate():
    payload = request.get_json(silent=True) or {}
    code, body, _hdrs = _daemon_request("POST", "/live/navigate", payload)
    auth.audit("live_navigate", target=payload.get("url"), detail=f"http={code}")
    return Response(body, status=code, mimetype="application/json")


@bp.route("/api/admin/live/prepare_checkout", methods=["POST"])
@auth.super_admin_required
def live_prepare_checkout():
    payload = request.get_json(silent=True) or {}
    # Longer timeout: prepare includes login + cart + checkout fill.
    code, body, _hdrs = _daemon_request("POST", "/live/prepare_checkout", payload, timeout=180)
    auth.audit(
        "live_prepare_checkout",
        target=f"{payload.get('org_key')} -> {payload.get('truck_url')}",
        detail=f"http={code}",
    )
    return Response(body, status=code, mimetype="application/json")


@bp.route("/api/admin/live/place_order", methods=["POST"])
@auth.super_admin_required
def live_place_order():
    code, body, _hdrs = _daemon_request("POST", "/live/place_order", timeout=60)
    auth.audit("live_place_order", target="-", detail=f"http={code}")
    return Response(body, status=code, mimetype="application/json")


# ============================================================
# Checkout screenshots (gallery + lightbox)
# ============================================================

# Folders we're willing to expose. Each must be a direct child of WORKDIR;
# the listing + serve endpoints both reject anything that resolves outside.
SCREENSHOT_DIRS = ("browser_screenshots", "checkout_screenshots", "test_run_screenshots")


def _screenshot_roots() -> list[tuple[str, Path]]:
    workdir = Path(WORKDIR).resolve()
    return [(name, (workdir / name).resolve()) for name in SCREENSHOT_DIRS]


@bp.route("/api/admin/screenshots", methods=["GET"])
@auth.login_required
def list_screenshots():
    """Inventory of all PNGs under the allowed screenshot dirs, newest first.

    Each entry's `path` is the workdir-relative path the file endpoint expects,
    so the UI doesn't have to reassemble it.
    """
    items: list[dict] = []
    workdir = Path(WORKDIR).resolve()
    for source, root in _screenshot_roots():
        if not root.exists():
            continue
        for p in root.rglob("*.png"):
            try:
                rel = p.resolve().relative_to(workdir).as_posix()
                stat = p.stat()
            except (OSError, ValueError):
                continue
            items.append({
                "source": source,
                "name": p.name,
                "path": rel,
                "size": stat.st_size,
                "mtime": datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
            })
    items.sort(key=lambda it: it["mtime"], reverse=True)
    return jsonify({"data": items, "count": len(items)})


@bp.route("/api/admin/screenshots/file", methods=["GET"])
@auth.login_required
def get_screenshot_file():
    """Serve a single screenshot. The `p` query arg is a workdir-relative path
    that must resolve under one of SCREENSHOT_DIRS — anything else (absolute
    paths, `..` escape attempts) gets 400."""
    rel = request.args.get("p", "").strip()
    if not rel:
        return jsonify({"success": False, "error": "missing p"}), 400

    workdir = Path(WORKDIR).resolve()
    try:
        requested = (workdir / rel).resolve()
    except (OSError, ValueError):
        return jsonify({"success": False, "error": "invalid path"}), 400

    if not any(
        _is_within(requested, root)
        for _name, root in _screenshot_roots()
    ):
        return jsonify({"success": False, "error": "invalid path"}), 400
    if not requested.exists() or not requested.is_file():
        return jsonify({"success": False, "error": "file missing"}), 404

    return Response(
        requested.read_bytes(),
        mimetype="image/png",
        headers={"Cache-Control": "private, max-age=300"},
    )


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


# Screenshots are bucketed by the scan that triggered them. Scans run every
# ~60s; a checkout typically completes inside the same minute but a slow
# Playwright flow can spill past it — extend the window by a small grace so
# we don't lose late-arriving frames to the next scan's bucket.
_BUCKET_GRACE_SECONDS = 30
_ET_TZ = timezone(timedelta(hours=-4))  # EDT; ET-naive scan strings are wall time

# Capture JSON files live next to screenshots — same naming convention,
# same bucketing strategy.
CAPTURE_DIR = Path(WORKDIR) / "checkout_captures"


@bp.route("/api/admin/scans/captures", methods=["GET"])
@auth.login_required
def list_captures():
    """Metadata for every checkout capture JSON, newest first.

    Each entry summarizes outcome + truck + start time, deferring the full
    network/console/HTML payload to /api/admin/scans/captures/file. The
    list is cheap to render; the bodies are large.
    """
    items: list[dict] = []
    if CAPTURE_DIR.exists():
        for p in CAPTURE_DIR.glob("*.json"):
            try:
                stat = p.stat()
            except OSError:
                continue
            # Peek at outcome / truck without loading the entire body.
            outcome = ""
            truck = ""
            engine = ""
            try:
                with open(p) as f:
                    head = f.read(2048)
                # JSON keys are at the top; cheap string search is enough.
                import re as _re
                m = _re.search(r'"outcome"\s*:\s*"([^"]+)"', head)
                outcome = m.group(1) if m else ""
                m = _re.search(r'"truck_name"\s*:\s*"([^"]+)"', head)
                truck = m.group(1) if m else ""
                m = _re.search(r'"engine"\s*:\s*"([^"]+)"', head)
                engine = m.group(1) if m else "script"
            except Exception:
                pass
            items.append({
                "name":    p.name,
                "path":    f"checkout_captures/{p.name}",
                "size":    stat.st_size,
                "mtime":   datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(),
                "outcome": outcome,
                "truck":   truck,
                "engine":  engine,
            })
    items.sort(key=lambda it: it["mtime"], reverse=True)
    return jsonify({"data": items, "count": len(items)})


@bp.route("/api/admin/scans/captures/file", methods=["GET"])
@auth.login_required
def get_capture_file():
    """Stream a single capture JSON. Same path-traversal guard as the
    screenshot endpoint — the resolved path must sit inside CAPTURE_DIR."""
    rel = (request.args.get("p") or "").strip()
    if not rel:
        return jsonify({"success": False, "error": "missing p"}), 400
    workdir = Path(WORKDIR).resolve()
    try:
        requested = (workdir / rel).resolve()
    except (OSError, ValueError):
        return jsonify({"success": False, "error": "invalid path"}), 400
    if not _is_within(requested, CAPTURE_DIR.resolve()):
        return jsonify({"success": False, "error": "invalid path"}), 400
    if not requested.exists() or not requested.is_file():
        return jsonify({"success": False, "error": "file missing"}), 404
    return Response(
        requested.read_bytes(),
        mimetype="application/json",
        headers={"Cache-Control": "private, max-age=60"},
    )


@bp.route("/api/admin/scans/captures/by-scan", methods=["GET"])
@auth.login_required
def captures_by_scan():
    """Bucket capture JSONs by the scan that triggered them — mirrors the
    screenshot by-scan endpoint so the UI can show both alongside each scan
    row in one consistent way."""
    limit = int(request.args.get("limit", "200"))
    scan_ts_strs: list[str] = []
    try:
        with get_conn() as c:
            for r in c.execute(
                "SELECT ts FROM scans ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall():
                scan_ts_strs.append(r["ts"])
    except Exception:
        scan_ts_strs = []
    scan_ts_strs.reverse()
    parsed = [(s, _parse_scan_ts(s)) for s in scan_ts_strs]
    parsed = [(k, d) for k, d in parsed if d is not None]
    keys  = [k for k, _ in parsed]
    edges = [d for _, d in parsed]

    buckets: dict[str, list[dict]] = {k: [] for k in keys}
    orphan: list[dict] = []
    import re as _re_local
    if CAPTURE_DIR.exists():
        for p in CAPTURE_DIR.glob("*.json"):
            try:
                stat = p.stat()
            except OSError:
                continue
            mtime = datetime.fromtimestamp(stat.st_mtime, UTC)
            # Peek for outcome + engine so the UI can color the entry.
            outcome = ""
            engine = "script"
            try:
                with open(p) as fh:
                    head = fh.read(2048)
                m = _re_local.search(r'"outcome"\s*:\s*"([^"]+)"', head)
                outcome = m.group(1) if m else ""
                m = _re_local.search(r'"engine"\s*:\s*"([^"]+)"', head)
                engine = m.group(1) if m else "script"
            except Exception:
                pass
            entry = {
                "name":    p.name,
                "path":    f"checkout_captures/{p.name}",
                "size":    stat.st_size,
                "mtime":   mtime.isoformat(),
                "outcome": outcome,
                "engine":  engine,
            }
            assigned = False
            for i in range(len(edges) - 1, -1, -1):
                start = edges[i]
                end_grace = (
                    edges[i + 1] + timedelta(seconds=_BUCKET_GRACE_SECONDS)
                    if i + 1 < len(edges)
                    else mtime + timedelta(seconds=1)
                )
                if start <= mtime < end_grace:
                    buckets[keys[i]].append(entry)
                    assigned = True
                    break
            if not assigned:
                orphan.append(entry)
    for v in buckets.values():
        v.sort(key=lambda e: e["mtime"], reverse=True)
    orphan.sort(key=lambda e: e["mtime"], reverse=True)
    total = sum(len(v) for v in buckets.values()) + len(orphan)
    return jsonify({"buckets": buckets, "orphan": orphan, "count": total})

def _parse_scan_ts(ts: str) -> datetime | None:
    """Scans table stores `%Y-%m-%d %H:%M:%S` naive ET strings (good360_monitor
    uses pytz.timezone('America/New_York') then strftime — see now_et_str).
    Anchor to EDT here; off by 1h during the winter switch but the bucketing
    grace covers that window comfortably."""
    if not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ET_TZ)
    except ValueError:
        return None


@bp.route("/api/admin/screenshots/by-scan", methods=["GET"])
@auth.login_required
def screenshots_by_scan():
    """Group screenshots by which scan triggered them.

    A screenshot whose mtime falls in [scan.ts, next_scan.ts + grace] belongs
    to that scan's bucket. Files older than the oldest scan or newer than the
    newest scan + grace go into 'orphan'. Returns the bucket keyed by the same
    scan ts string the frontend uses as its row key.
    """
    limit = int(request.args.get("limit", "200"))

    # Pull the scan timestamps once, oldest → newest.
    scan_ts_strs: list[str] = []
    try:
        with get_conn() as c:
            for r in c.execute(
                "SELECT ts FROM scans ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall():
                scan_ts_strs.append(r["ts"])
    except Exception:
        scan_ts_strs = []
    scan_ts_strs.reverse()
    parsed: list[tuple[str, datetime]] = []
    for s in scan_ts_strs:
        dt = _parse_scan_ts(s)
        if dt is not None:
            parsed.append((s, dt))

    # Pre-compute the window edges so each shot is one binary scan.
    edges = [dt for _key, dt in parsed]
    keys = [k for k, _dt in parsed]

    buckets: dict[str, list[dict]] = {k: [] for k in keys}
    orphan: list[dict] = []
    workdir = Path(WORKDIR).resolve()

    # Test-run screenshots are a separate workflow (Test buy panel has its own
    # per-run viewer), so excluding them from scan buckets avoids assigning a
    # test-buy click to whichever scan happened to be in flight at the time.
    autobuy_sources = {"browser_screenshots", "checkout_screenshots"}

    for source, root in _screenshot_roots():
        if source not in autobuy_sources:
            continue
        if not root.exists():
            continue
        for p in root.rglob("*.png"):
            try:
                rel = p.resolve().relative_to(workdir).as_posix()
                stat = p.stat()
            except (OSError, ValueError):
                continue
            mtime = datetime.fromtimestamp(stat.st_mtime, UTC)
            entry = {
                "source": source,
                "name":   p.name,
                "path":   rel,
                "size":   stat.st_size,
                "mtime":  mtime.isoformat(),
            }
            # Latest scan whose ts ≤ mtime. Linear scan from the back since
            # scan count is small and we expect mtimes to land near the end.
            assigned = False
            for i in range(len(edges) - 1, -1, -1):
                start = edges[i]
                end_grace = (
                    edges[i + 1] + timedelta(seconds=_BUCKET_GRACE_SECONDS)
                    if i + 1 < len(edges)
                    else mtime + timedelta(seconds=1)  # newest scan: no upper bound
                )
                if start <= mtime < end_grace:
                    buckets[keys[i]].append(entry)
                    assigned = True
                    break
            if not assigned:
                orphan.append(entry)

    # Sort each bucket newest-first for natural lightbox order.
    for v in buckets.values():
        v.sort(key=lambda e: e["mtime"], reverse=True)
    orphan.sort(key=lambda e: e["mtime"], reverse=True)

    total = sum(len(v) for v in buckets.values()) + len(orphan)
    return jsonify({"buckets": buckets, "orphan": orphan, "count": total})


# ============================================================
# Scans, purchases, audit (read-only views)
# ============================================================

def _read_scans(limit: int, include_trucks: bool = True) -> list[dict]:
    """Return the most recent scans, preferring the SQL `scans` table.
    Falls back to good360_run_log.json if SQL is empty (e.g., immediately
    after migration before the monitor's next scan writes a row).

    `include_trucks=False` skips the heavy trucks_json blob (the main scans
    tab only needs truck_count/available_count + the precomputed `title`).
    """
    rows: list[dict] = []
    try:
        with get_conn() as c:
            if include_trucks:
                sql_rows = c.execute(
                    "SELECT ts, alert_sent, action, truck_count, available_count, "
                    "trucks_json FROM scans ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                # Heavy: trucks_json blobs add up to 200KB+ on a 200-row pull
                # while the list view never reads them. The only thing the JS
                # needs out of the blob is the lead truck's name — derive it
                # from a lightweight SUBSTR scan below if at all needed.
                sql_rows = c.execute(
                    "SELECT ts, alert_sent, action, truck_count, available_count "
                    "FROM scans ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        for r in sql_rows:
            entry = {
                "time":            r["ts"],
                "alert_sent":      bool(r["alert_sent"]),
                "action":          r["action"] or "",
                "truck_count":     r["truck_count"],
                "available_count": r["available_count"],
                "status":          "scanned",
            }
            if include_trucks:
                try:
                    trucks = json.loads(r["trucks_json"] or "[]")
                except (json.JSONDecodeError, TypeError):
                    trucks = []
                entry["trucks"] = trucks
                entry["title"]  = trucks[0]["name"] if trucks else None
            else:
                entry["title"] = None
            rows.append(entry)
    except Exception:
        rows = []
    if rows:
        _stitch_login_to_scans(rows)
        return rows
    # JSON fallback — old single-file format.
    rl = _safe_json(RUN_LOG)
    runs = rl.get("runs", rl) if isinstance(rl, dict) else (rl or [])
    out: list[dict] = []
    for entry in runs[-limit:][::-1]:
        out.append({
            "time":            entry.get("time"),
            "alert_sent":      bool(entry.get("alert_sent")),
            "action":          entry.get("action") or "",
            "trucks":          entry.get("trucks") or [],
            "truck_count":     len(entry.get("trucks") or []),
            "available_count": sum(1 for t in (entry.get("trucks") or []) if t.get("available")),
            "title":           (entry.get("trucks") or [{}])[0].get("name") if entry.get("trucks") else None,
            "status":          "scanned",
        })
    _stitch_login_to_scans(out)
    return out


def _stitch_login_to_scans(scans: list[dict]) -> None:
    """Attach login_ok/email/error to each scan by matching the closest row
    in good360_login_attempts within ±15s.

    The monitor records both timestamps: scans.ts is ET-naive (e.g.
    "2026-05-12 13:28:22") and good360_login_attempts.ts is UTC-naive
    (sqlite's datetime('now')). We parse scan ts as ET, convert to UTC,
    then range-query the login table once per request — cheap enough to
    run on every /api/admin/scans call.

    Mutates the input list in place.
    """
    if not scans:
        return

    parsed: list[tuple[dict, datetime]] = []
    for s in scans:
        t = s.get("time")
        if not t:
            continue
        try:
            dt_et = datetime.strptime(t, "%Y-%m-%d %H:%M:%S").replace(tzinfo=_ET_TZ)
            parsed.append((s, dt_et.astimezone(UTC)))
        except (ValueError, TypeError):
            continue
    if not parsed:
        return

    window_seconds = 20
    min_t = min(u for _s, u in parsed) - timedelta(seconds=window_seconds)
    max_t = max(u for _s, u in parsed) + timedelta(seconds=window_seconds)

    fmt = "%Y-%m-%d %H:%M:%S"
    try:
        with get_conn() as c:
            rows = c.execute(
                "SELECT ts, source, email, success, error "
                "FROM good360_login_attempts "
                "WHERE source = 'monitor' AND ts >= ? AND ts <= ? "
                "ORDER BY ts ASC",
                (min_t.strftime(fmt), max_t.strftime(fmt)),
            ).fetchall()
    except Exception:
        return

    attempts: list[tuple[datetime, dict]] = []
    for r in rows:
        try:
            ts_utc = datetime.strptime(r["ts"], fmt).replace(tzinfo=UTC)
            attempts.append((ts_utc, r))
        except (ValueError, TypeError):
            continue
    if not attempts:
        return

    best_delta = timedelta(seconds=15)
    for scan, scan_utc in parsed:
        nearest = None
        nearest_delta = best_delta
        for a_ts, a_row in attempts:
            d = abs(a_ts - scan_utc)
            if d <= nearest_delta:
                nearest = a_row
                nearest_delta = d
        if nearest is not None:
            scan["login_ok"] = bool(nearest["success"])
            scan["login_email"] = nearest["email"]
            scan["login_error"] = nearest["error"]


# Ignore-list lives as a JSON file in the workdir volume so the monitor
# container (separate Python process, no DB import) can read it cheaply
# on every scan without an HTTP roundtrip back to this service.
IGNORED_PRODUCTS_FILE = Path(WORKDIR) / "ignored_products.json"


def _load_ignored_products() -> set[str]:
    try:
        data = json.loads(IGNORED_PRODUCTS_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    except (OSError, json.JSONDecodeError):
        return set()


def _save_ignored_products(names: set[str]) -> None:
    IGNORED_PRODUCTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = IGNORED_PRODUCTS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(sorted(names), indent=2), encoding="utf-8")
    tmp.replace(IGNORED_PRODUCTS_FILE)


def _is_sandbox_mode() -> bool:
    return (os.environ.get("SANDBOX_MODE", "") or "").strip().lower() in ("1","true","yes","on")


def _url_belongs_to_mode(url: str | None, sandbox: bool) -> bool:
    """Whether a tracked_product URL matches the current run mode.
    Sandbox URLs contain sandbox-360.netlify.app; live URLs hit
    catalog.good360.org. Unknown URLs (None / empty) are shown in both
    so an operator can still see legacy data."""
    if not url:
        return True
    u = url.lower()
    if sandbox:
        return "sandbox-360" in u or "sandbox" in u
    return "good360.org" in u and "sandbox" not in u


@bp.route("/api/admin/scans/products", methods=["GET"])
@auth.login_required
def admin_scan_products():
    """Observed products list, joined with the per-product flags table.

    Rows are pulled from tracked_products (the durable record of every truck
    name the monitor has ever seen). Observation/availability counts come
    from scans.trucks_json aggregated on the fly. URL family is filtered
    to the current SANDBOX_MODE so sandbox + live data don't bleed into
    each other.
    """
    sandbox = _is_sandbox_mode()
    ignored = _load_ignored_products()

    # Build observation counts from recent scans so the page shows recency
    # without a separate column. 500 scans ≈ last 8 hours.
    counts: dict[str, dict] = {}
    for s in _read_scans(int(request.args.get("limit", "500"))):
        ts = s.get("time")
        for t in s.get("trucks") or []:
            name = (t.get("name") or "").strip()
            if not name:
                continue
            d = counts.setdefault(name, {"obs": 0, "avail": 0, "last_seen": ts})
            d["obs"] += 1
            if t.get("available"):
                d["avail"] += 1
            if ts and (not d["last_seen"] or ts > d["last_seen"]):
                d["last_seen"] = ts

    out: list[dict] = []
    with get_conn() as c:
        rows = c.execute(
            "SELECT name, tracked, autobuy_enabled, last_url, last_price, "
            "       manual_price, description, first_seen, last_seen, updated_at "
            "FROM tracked_products "
            "ORDER BY last_seen DESC NULLS LAST, name"
        ).fetchall()
    for r in rows:
        if not _url_belongs_to_mode(r["last_url"], sandbox):
            continue
        agg = counts.get(r["name"], {})
        # Manual override beats scrape — the scan account can't see prices
        # on the live catalog, so the operator sets the price by hand and
        # we surface that everywhere as the canonical price.
        manual = r["manual_price"]
        scraped = r["last_price"]
        out.append({
            "name":             r["name"],
            "price":            manual if manual is not None else scraped,
            "manual_price":     manual,
            "scraped_price":    scraped,
            "price_source":     "manual" if manual is not None else ("scraped" if scraped is not None else "none"),
            "tracked":          bool(r["tracked"]),
            "autobuy_enabled":  bool(r["autobuy_enabled"]),
            "ignored":          r["name"] in ignored,
            "description":      r["description"] or "",
            "last_url":         r["last_url"] or "",
            "observations":     agg.get("obs", 0),
            "available_count":  agg.get("avail", 0),
            "first_seen":       r["first_seen"],
            "last_seen":        r["last_seen"],
            "updated_at":       r["updated_at"],
        })

    return jsonify({
        "data": out,
        "count": len(out),
        "mode": "sandbox" if sandbox else "live",
    })


def _set_product_field(name: str, field: str, value):
    """Idempotent UPDATE for a single tracked_products field. Returns whether
    a row matched."""
    assert field in {"tracked", "autobuy_enabled", "description", "manual_price"}
    with get_conn() as c:
        cur = c.execute(
            f"UPDATE tracked_products SET {field} = ?, updated_at = datetime('now') "
            f"WHERE name = ?",
            (value, name),
        )
        return cur.rowcount > 0


@bp.route("/api/admin/scans/products/tracked", methods=["POST"])
@auth.super_admin_required
def admin_set_product_tracked():
    """Tracking flag is now the single off-switch. When tracked is turned
    off the row is grayed out, no alerts fire, no autobuy runs — and we
    force autobuy_enabled=0 in the same UPDATE so the two flags can't drift
    apart (e.g. tracked=off + autobuy=on would be a contradiction)."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    tracked = 1 if bool(body.get("tracked")) else 0
    if not name:
        return jsonify({"success": False, "error": "name required"}), 400
    with get_conn() as c:
        if tracked == 0:
            cur = c.execute(
                "UPDATE tracked_products "
                "SET tracked = 0, autobuy_enabled = 0, updated_at = datetime('now') "
                "WHERE name = ?",
                (name,),
            )
        else:
            cur = c.execute(
                "UPDATE tracked_products "
                "SET tracked = 1, updated_at = datetime('now') "
                "WHERE name = ?",
                (name,),
            )
        if cur.rowcount == 0:
            return jsonify({"success": False, "error": "not found"}), 404
    auth.audit("set_product_tracked", target=name, detail=str(tracked))
    return jsonify({
        "success":         True,
        "name":            name,
        "tracked":         bool(tracked),
        "autobuy_enabled": False if tracked == 0 else None,  # None = unchanged
    })


@bp.route("/api/admin/scans/products/autobuy", methods=["POST"])
@auth.super_admin_required
def admin_set_product_autobuy():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    autobuy = 1 if bool(body.get("autobuy_enabled")) else 0
    if not name:
        return jsonify({"success": False, "error": "name required"}), 400
    ok = _set_product_field(name, "autobuy_enabled", autobuy)
    if not ok:
        return jsonify({"success": False, "error": "not found"}), 404
    auth.audit("set_product_autobuy", target=name, detail=str(autobuy))
    return jsonify({"success": True, "name": name, "autobuy_enabled": bool(autobuy)})


@bp.route("/api/admin/scans/products/fetch-price", methods=["POST"])
@auth.super_admin_required
def admin_fetch_product_price():
    """Ask the daemon to drive its live browser through cart/checkout for
    `name` and read back the price Good360 hides on the listing. The
    daemon needs both an org_key (for credentials) and the product URL;
    we look both up from tracked_products. Saves the result as last_price
    on success so the Price column updates automatically."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "name required"}), 400
    with get_conn() as c:
        row = c.execute(
            "SELECT last_url FROM tracked_products WHERE name = ?",
            (name,),
        ).fetchone()
    if not row or not row["last_url"]:
        return jsonify({"success": False, "error": "product has no known URL — wait for the next scan or pick a different product"}), 404

    # Pick an org_key with valid credentials. In sandbox we use the legacy
    # config; in live, any active org in load_orgs works because the daemon
    # routes through sandbox.org_credentials anyway.
    try:
        import sys as _sys
        if "/app" not in _sys.path:
            _sys.path.insert(0, "/app")
        import config as _cfg
        orgs = _cfg.load_orgs()
    except Exception as e:
        return jsonify({"success": False, "error": f"orgs load failed: {e}"}), 500
    if not orgs:
        return jsonify({"success": False, "error": "no orgs configured"}), 500
    org_key = next(iter(orgs.keys()))

    code, body_bytes, _hdrs = _daemon_request(
        "POST", "/live/fetch_price",
        {"org_key": org_key, "truck_url": row["last_url"]},
        timeout=240,
    )
    try:
        result = json.loads(body_bytes or b"{}")
    except Exception:
        result = {"status": "ERROR", "message": "daemon returned non-JSON"}
    price = result.get("price")
    daemon_status = result.get("status") or "ERROR"

    if isinstance(price, (int, float)) and price > 0:
        with get_conn() as c:
            c.execute(
                "UPDATE tracked_products "
                "SET last_price = ?, updated_at = datetime('now') "
                "WHERE name = ?",
                (float(price), name),
            )
        auth.audit("fetch_product_price", target=name, detail=f"${price:.2f}")
        return jsonify({"success": True, "name": name, "price": float(price),
                        "status": daemon_status,
                        "message": result.get("message", "")})

    auth.audit("fetch_product_price", target=name, detail=f"failed: {daemon_status}")
    return jsonify({
        "success": False,
        "name": name,
        "status": daemon_status,
        "message": result.get("message") or "no price returned (truck may not be currently available)",
    }), 200


@bp.route("/api/admin/scans/products/price", methods=["POST"])
@auth.super_admin_required
def admin_set_product_price():
    """Set or clear the operator's manual price for a product. Body:
    {name, price: number | null}. NULL clears the override and falls
    back to the (usually-NULL) scraped value."""
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    raw  = body.get("price")
    if not name:
        return jsonify({"success": False, "error": "name required"}), 400
    if raw is None or raw == "":
        price_val = None
    else:
        try:
            price_val = float(raw)
            if price_val < 0:
                raise ValueError("negative")
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "price must be a non-negative number or null"}), 400
    ok = _set_product_field(name, "manual_price", price_val)
    if not ok:
        return jsonify({"success": False, "error": "not found"}), 404
    auth.audit("set_product_price", target=name, detail=str(price_val))
    return jsonify({"success": True, "name": name, "manual_price": price_val})


@bp.route("/api/admin/scans/products/description", methods=["POST"])
@auth.super_admin_required
def admin_set_product_description():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    desc = (body.get("description") or "").strip()
    if not name:
        return jsonify({"success": False, "error": "name required"}), 400
    ok = _set_product_field(name, "description", desc)
    if not ok:
        return jsonify({"success": False, "error": "not found"}), 404
    auth.audit("set_product_description", target=name, detail=f"len={len(desc)}")
    return jsonify({"success": True, "name": name, "description": desc})


@bp.route("/api/admin/scans/products/ignore", methods=["POST"])
@auth.login_required
def admin_set_product_ignore():
    """Toggle ignore status for a product. Body: {name: str, ignored: bool}.

    Ignored products are skipped by the monitor's autobuy ladder (alert-only).
    Persisted to workdir/ignored_products.json so the monitor sees the change
    on its next scan without a restart.
    """
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    ignored = bool(body.get("ignored"))
    if not name:
        return jsonify({"success": False, "error": "name required"}), 400

    current = _load_ignored_products()
    was_ignored = name in current
    if ignored:
        current.add(name)
    else:
        current.discard(name)
    _save_ignored_products(current)

    auth.audit(
        "set_product_ignore",
        target=name,
        detail=f"{was_ignored}->{ignored}",
    )
    return jsonify({"success": True, "name": name, "ignored": ignored})


def _conditional_json(etag: str, build_payload, cache_seconds: int = 0):
    """Honor If-None-Match: return 304 (no body) when the client's cached
    ETag matches; otherwise build and return the JSON payload with the
    current ETag attached. `cache_seconds` sets max-age for browser/proxy
    caching — 0 forces revalidation on every poll (recommended for our
    auth'd JSON; the ETag still skips the body 99% of the time)."""
    inm = (request.headers.get("If-None-Match") or "").strip()
    quoted = f'"{etag}"'
    if inm == quoted or inm == etag:
        resp = Response(status=304)
        resp.headers["ETag"] = quoted
        return resp
    resp = jsonify(build_payload())
    resp.headers["ETag"] = quoted
    resp.headers["Cache-Control"] = (
        f"private, max-age={cache_seconds}" if cache_seconds else "no-cache"
    )
    return resp


def _scans_etag(limit: int, slim: bool, count_only: bool) -> str:
    """Cheap version-tag: changes when the newest scan id changes, when the
    heartbeat file is rewritten, or when query shape changes. SQLite max(id)
    is an O(1) index lookup."""
    try:
        with get_conn() as c:
            row = c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM scans").fetchone()
            max_id = int(row["m"]) if row else 0
    except Exception:
        max_id = 0
    try:
        hb_mtime = int(os.path.getmtime(HEARTBEAT)) if os.path.exists(HEARTBEAT) else 0
    except OSError:
        hb_mtime = 0
    raw = f"{max_id}:{hb_mtime}:{limit}:{int(slim)}:{int(count_only)}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


@bp.route("/api/admin/scans", methods=["GET"])
@auth.login_required
def admin_scans():
    """Recent scan activity. Reads from the SQL scans table (preferred)
    or the legacy good360_run_log.json file as a transitional fallback.

    Query params:
      limit       — number of rows (default 100, capped at 1000)
      slim=1      — omit per-scan trucks_json (≈10× smaller payload). The main
                    dashboard poll uses this; the alert poller does not.
      count_only=1 — return just {count: N} for the sidebar badge.
    """
    limit = max(1, min(request.args.get("limit", 100, type=int), 1000))
    slim = request.args.get("slim") in ("1", "true", "yes")
    count_only = request.args.get("count_only") in ("1", "true", "yes")

    etag = _scans_etag(limit, slim, count_only)

    def _build():
        if count_only:
            try:
                with get_conn() as c:
                    n = c.execute("SELECT COUNT(*) AS n FROM scans").fetchone()["n"]
            except Exception:
                n = 0
            return {"success": True, "data": {"count": int(n)}}

        heartbeat = _safe_json(HEARTBEAT)
        scans = _read_scans(limit, include_trucks=not slim)
        services = _docker_service_states([
            "monitor", "daemon", "watchdog", "telegram-bot", "missioncontrol", "intake",
        ])
        return {
            "success": True,
            "data": {
                "scans": scans,
                "heartbeat": heartbeat,
                "services": services,
            },
        }

    return _conditional_json(etag, _build)


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

    # ETag from the last line + line count — cheap and changes precisely when
    # new output appears. With _docker_logs cached for 3s, repeat polls within
    # that window all share the same ETag and short-circuit to 304.
    tag_seed = (raw_lines[-1] if raw_lines else "") + f"|{len(raw_lines)}|{service}|{n}"
    etag = hashlib.sha1(tag_seed.encode("utf-8", errors="replace")).hexdigest()[:16]

    def _build():
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

        return {
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
        }

    return _conditional_json(etag, _build)


COMPOSE_PROJECT = os.environ.get("COMPOSE_PROJECT_NAME", "good-360-bids")


# Tiny TTL cache so the dashboard's 5-second poll loop doesn't fire a
# fresh `docker compose logs` subprocess on every request. We accept a few
# seconds of staleness in exchange for keeping the server responsive when
# multiple operators have the dashboard open at once.
_DOCKER_LOGS_CACHE: dict[tuple[str, int], tuple[float, list[str]]] = {}
_DOCKER_LOGS_TTL = 3.0   # seconds
_DOCKER_LOGS_LOCK = threading.Lock()


def _docker_logs(service: str, n: int) -> list[str]:
    """Read the last `n` log lines from a compose service via the docker CLI.
    Cached for `_DOCKER_LOGS_TTL` seconds per (service, n) to avoid spawning
    a subprocess on every dashboard poll."""
    if not shutil.which("docker"):
        return []
    key = (service, n)
    now = time.monotonic()
    with _DOCKER_LOGS_LOCK:
        cached = _DOCKER_LOGS_CACHE.get(key)
        if cached and (now - cached[0]) < _DOCKER_LOGS_TTL:
            return cached[1]
    try:
        proc = subprocess.run(
            ["docker", "compose", "-p", COMPOSE_PROJECT,
             "-f", "/app/docker-compose.yml", "logs",
             "--no-color", "--tail", str(n), service],
            # Tighter timeout than before — a hung docker call shouldn't
            # take 10s to give up while users are watching the dashboard.
            capture_output=True, text=True, timeout=4, cwd="/app",
        )
        if proc.returncode != 0:
            lines = []
        else:
            lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    except Exception:
        lines = []
    with _DOCKER_LOGS_LOCK:
        _DOCKER_LOGS_CACHE[key] = (now, lines)
    return lines


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


# `docker compose ps` forks a subprocess and parses container metadata — 50-
# 300ms of work per call. The scans endpoint asks for it on every poll, so
# we cache the parsed result. Services don't change state minute-to-minute
# in steady state, so a 10s TTL is well within operator expectations.
_DOCKER_SVC_CACHE: dict[tuple[str, ...], tuple[float, list[dict]]] = {}
_DOCKER_SVC_TTL = 10.0  # seconds
_DOCKER_SVC_LOCK = threading.Lock()


def _docker_service_states(names: list[str]) -> list[dict]:
    """Inspect docker for the given compose services. Best-effort.

    Treats `restarting` as `running` for services like `monitor` whose normal
    operating mode is run-once-then-exit (compose's `restart: always` brings
    it back). The user sees "running" for healthy services and the log tail
    lets them spot a crash-loop separately.
    """
    key = tuple(names)
    now = time.monotonic()
    with _DOCKER_SVC_LOCK:
        cached = _DOCKER_SVC_CACHE.get(key)
        if cached and (now - cached[0]) < _DOCKER_SVC_TTL:
            return cached[1]

    if not shutil.which("docker"):
        result = [{"name": n, "state": "unknown", "running": None} for n in names]
        with _DOCKER_SVC_LOCK:
            _DOCKER_SVC_CACHE[key] = (now, result)
        return result
    try:
        proc = subprocess.run(
            ["docker", "compose", "-p", COMPOSE_PROJECT,
             "-f", "/app/docker-compose.yml", "ps", "-a", "--format", "json"],
            # Tighter timeout — a hung docker probe shouldn't block a poll.
            capture_output=True, text=True, timeout=4, cwd="/app",
        )
        if proc.returncode != 0:
            result = [{"name": n, "state": "unknown", "running": None} for n in names]
            with _DOCKER_SVC_LOCK:
                _DOCKER_SVC_CACHE[key] = (now, result)
            return result
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
        with _DOCKER_SVC_LOCK:
            _DOCKER_SVC_CACHE[key] = (now, result)
        return result
    except Exception:
        result = [{"name": n, "state": "unknown", "running": None} for n in names]
        with _DOCKER_SVC_LOCK:
            _DOCKER_SVC_CACHE[key] = (now, result)
        return result


ROSTER_DB_PATH = os.environ.get(
    "ROSTER_DB_PATH",
    "/app/good360_roster/db/roster.db",
)


def _roster_purchase_rows(where_sql: str = "", params: tuple = (), limit: int = 200,
                          days: int | None = None, q: str | None = None,
                          customer_id: str | None = None,
                          since: str | None = None, until: str | None = None) -> list[dict]:
    """Read joined purchase_attempts + truck_events + nonprofits rows from
    roster.db. Returns rows shaped to the existing /api/admin/purchases
    contract (ts, org_id, truck, total, status, detail) so the UI doesn't
    care that the source switched from JSONL to SQL.

    `where_sql` lets callers filter (e.g. by quickbeed_customer_id). It must
    start with "AND" if non-empty. `q` does a case-insensitive LIKE over
    truck title, org name, and confirmation number. `since`/`until` are
    ISO date strings ('YYYY-MM-DD'); when set they override `days`. Empty
    rows are returned as-is when roster.db is unreachable so the UI can
    render an empty state instead of a 500."""
    import sqlite3 as _sqlite
    if not os.path.exists(ROSTER_DB_PATH):
        return []
    # The caller's `params` are bound FIRST, so the caller's where_sql
    # clause must also come first — appending it after the date/q/customer
    # clauses crossed the placeholders (the customer id ended up inside
    # datetime('now', ?), silently matching nothing: empty buyer history).
    where_parts = ["1=1"]
    if where_sql:
        where_parts.append(where_sql)
    if since:
        where_parts.append("pa.started_at >= ?")
        params = (*params, since)
    if until:
        # Inclusive upper bound — extend to end-of-day so a single date
        # picker behaves intuitively.
        where_parts.append("pa.started_at <= ?")
        params = (*params, f"{until} 23:59:59")
    if (not since and not until) and days and days > 0:
        where_parts.append("pa.started_at >= datetime('now', ?)")
        params = (*params, f"-{days} day")
    if q:
        like = f"%{q}%"
        where_parts.append(
            "(te.truck_title LIKE ? OR np.org_name LIKE ? OR pa.confirmation_number LIKE ?)"
        )
        params = (*params, like, like, like)
    if customer_id:
        where_parts.append("np.quickbeed_customer_id = ?")
        params = (*params, customer_id)
    where = " AND ".join(where_parts)
    sql = f"""
        SELECT
            pa.id                  AS attempt_id,
            COALESCE(pa.completed_at, pa.started_at) AS ts,
            pa.started_at          AS started_at,
            pa.completed_at        AS completed_at,
            pa.status              AS status,
            pa.mode                AS mode,
            pa.attempt_number      AS attempt_number,
            pa.error_message       AS error_message,
            pa.screenshot_path     AS screenshot_path,
            pa.confirmation_number AS confirmation_number,
            pa.order_total         AS order_total,
            pa.cooldown_applied    AS cooldown_applied,
            te.truck_title         AS truck_title,
            te.truck_url           AS truck_url,
            te.truck_price         AS truck_price,
            np.org_name            AS org_name,
            np.contact_email       AS org_email,
            np.quickbeed_customer_id AS quickbeed_customer_id
        FROM purchase_attempts pa
        LEFT JOIN truck_events te ON te.id = pa.truck_event_id
        LEFT JOIN nonprofits   np ON np.id = pa.nonprofit_id
        WHERE {where}
        ORDER BY COALESCE(pa.completed_at, pa.started_at) DESC
        LIMIT ?
    """
    try:
        conn = _sqlite.connect(ROSTER_DB_PATH, timeout=5.0)
        conn.row_factory = _sqlite.Row
        rows = conn.execute(sql, (*params, limit)).fetchall()
        conn.close()
    except _sqlite.Error:
        return []

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        # UI-compatible aliases (keep the rich fields too)
        d["ts"]      = d.get("ts") or d.get("started_at")
        d["org_id"]  = d.get("org_name") or "—"
        d["truck"]   = d.get("truck_title") or "—"
        d["total"]   = d.get("order_total") if d.get("order_total") is not None else d.get("truck_price")
        d["detail"]  = d.get("error_message") or d.get("confirmation_number") or ""
        # Drop nothing — caller can pick. Some keys (like 'event') are absent
        # since the JSONL layer used 'event' to disambiguate; the SQL layer
        # uses 'status' which is more precise.
        out.append(d)
    return out


def _legacy_purchase_rows(limit: int = 200, days: int | None = None,
                          q: str | None = None, customer_id: str | None = None,
                          since: str | None = None, until: str | None = None) -> list[dict]:
    """Read every legacy autobuy attempt from dashboard.db.
    Shape-compatible with _roster_purchase_rows so the UI doesn't have to
    know which engine produced a row."""
    where_parts: list[str] = []
    params: list = []
    if since:
        where_parts.append("ts >= ?")
        params.append(since)
    if until:
        where_parts.append("ts <= ?")
        params.append(f"{until} 23:59:59")
    if (not since and not until) and days and days > 0:
        where_parts.append("ts >= datetime('now', ?)")
        params.append(f"-{days} day")
    if q:
        like = f"%{q}%"
        where_parts.append(
            "(truck_name LIKE ? OR customer_name LIKE ? OR org_name LIKE ? OR confirmation_number LIKE ?)"
        )
        params.extend([like, like, like, like])
    if customer_id:
        where_parts.append("customer_id = ?")
        params.append(customer_id)
    where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
    sql = f"""
        SELECT id, ts, status, engine, org_name, customer_id, customer_name,
               truck_name, truck_url, truck_price, order_total,
               confirmation_number, error_message, capture_path,
               screenshot_path, elapsed_seconds
        FROM legacy_purchase_attempts
        {where_sql}
        ORDER BY ts DESC
        LIMIT ?
    """
    params.append(limit)
    try:
        with get_conn() as c:
            rows = c.execute(sql, params).fetchall()
    except Exception:
        return []
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        # UI-compatible aliases — match the keys _roster_purchase_rows uses.
        d["attempt_id"] = d.pop("id", None)
        d["org_id"]     = d.get("customer_name") or d.get("org_name") or "—"
        d["truck"]      = d.get("truck_name") or "—"
        d["total"]      = d.get("order_total") if d.get("order_total") is not None else d.get("truck_price")
        d["detail"]     = (
            d.get("confirmation_number")
            or d.get("error_message")
            or ""
        )
        d["source"]     = "legacy"
        d["truck_title"] = d.get("truck_name")
        out.append(d)
    return out


@bp.route("/api/admin/purchases", methods=["GET"])
@auth.login_required
def admin_purchases():
    """Every purchase attempt — pass and fail — across both engines.

    Merges:
      - roster.db.purchase_attempts (the v2 autobuy_v2 path)
      - dashboard.db.legacy_purchase_attempts (good360_monitor.py via the
        legacy autobuy script / daemon / agent escalation)

    Tagged with `source` so the UI can show provenance. Sorted by ts desc.

    Query params:
      limit       — page size (default 50, max 1000)
      offset      — page offset for pagination (default 0)
      days        — restrict to last N days (default 14; ignored if since/until set)
      since       — ISO date 'YYYY-MM-DD' lower bound on attempt timestamp
      until       — ISO date 'YYYY-MM-DD' upper bound (inclusive end-of-day)
      q           — case-insensitive LIKE over truck name + org/customer name + confirmation #
      customer_id — exact match on the QuickBeed customer id
    """
    limit  = max(1, min(request.args.get("limit", 50, type=int), 1000))
    offset = max(0, request.args.get("offset", 0, type=int))
    days   = request.args.get("days", 14, type=int)
    since  = (request.args.get("since")  or "").strip() or None
    until  = (request.args.get("until")  or "").strip() or None
    q      = (request.args.get("q")      or "").strip() or None
    customer_id = (request.args.get("customer_id") or "").strip() or None

    # Pull all matching rows from each source up to a hard cap. With the
    # cap at 5K per source we cover practical pagination depths without
    # over-fetching.
    HARD_CAP = 5000
    roster_rows = _roster_purchase_rows(
        limit=HARD_CAP, days=days, q=q, customer_id=customer_id,
        since=since, until=until,
    )
    for r in roster_rows:
        r.setdefault("source", "roster")
    legacy_rows = _legacy_purchase_rows(
        limit=HARD_CAP, days=days, q=q, customer_id=customer_id,
        since=since, until=until,
    )

    # Merge + sort by ts desc (string compare is fine — both use ISO-ish
    # 'YYYY-MM-DD HH:MM:SS' from sqlite's datetime('now')).
    merged = roster_rows + legacy_rows
    merged.sort(key=lambda r: (r.get("ts") or ""), reverse=True)
    total = len(merged)

    # Aggregate stats over the FULL filtered set (not just the page) so
    # the metric strip stays accurate when the operator paginates. Status
    # canonicalization mirrors the dashboard's isSuccess/isFail helpers.
    SUCCESS = {"success", "dry_run_ok", "ok"}
    FAIL_HINTS = ("fail", "error", "missed", "abort", "timeout")
    ok_n = fail_n = 0
    spend = 0.0
    for r in merged:
        st = (r.get("status") or "").lower()
        if st in SUCCESS:
            ok_n += 1
            spend += float(r.get("order_total") or 0)
        elif any(h in st for h in FAIL_HINTS):
            fail_n += 1

    page = merged[offset:offset + limit]

    return jsonify({
        "success": True,
        "data":    page,
        "total":   total,
        "offset":  offset,
        "limit":   limit,
        "has_more": (offset + limit) < total,
        "counts":  {"roster": len(roster_rows), "legacy": len(legacy_rows), "total": total},
        "stats":   {"ok": ok_n, "fail": fail_n, "spend": round(spend, 2)},
    })


@bp.route("/api/admin/purchases/<source>/<int:attempt_id>", methods=["DELETE"])
@auth.super_admin_required
def admin_delete_purchase(source: str, attempt_id: int):
    """Hard-delete a single purchase attempt. Source must be 'roster' or
    'legacy' so we know which database to touch. Audit-logged."""
    source = source.lower()
    if source not in ("roster", "legacy"):
        return jsonify({"success": False, "error": "source must be roster|legacy"}), 400

    if source == "legacy":
        try:
            with get_conn() as c:
                c.execute("DELETE FROM legacy_purchase_attempts WHERE id = ?", (attempt_id,))
                deleted = c.total_changes
        except Exception as e:
            return jsonify({"success": False, "error": f"delete failed: {e}"}), 500
    else:
        import sqlite3 as _sqlite
        if not os.path.exists(ROSTER_DB_PATH):
            return jsonify({"success": False, "error": "roster.db unavailable"}), 503
        try:
            conn = _sqlite.connect(ROSTER_DB_PATH, timeout=5.0)
            try:
                cur = conn.execute("DELETE FROM purchase_attempts WHERE id = ?", (attempt_id,))
                conn.commit()
                deleted = cur.rowcount
            finally:
                conn.close()
        except _sqlite.Error as e:
            return jsonify({"success": False, "error": f"delete failed: {e}"}), 500

    if not deleted:
        return jsonify({"success": False, "error": "not found"}), 404

    auth.audit("purchase.delete", target=f"{source}:{attempt_id}",
               detail=f"deleted {deleted} row(s)")
    return jsonify({"success": True, "deleted": deleted})


def _load_purchase_row(source: str, attempt_id: int) -> dict | None:
    """Fetch a single purchase by (source, id), normalized to the same
    shape `_roster_purchase_rows` / `_legacy_purchase_rows` produce."""
    if source == "legacy":
        try:
            with get_conn() as c:
                row = c.execute(
                    "SELECT id AS attempt_id, ts, status, engine, org_name, "
                    "customer_id, customer_name, truck_name, truck_url, "
                    "truck_price, order_total, confirmation_number, "
                    "error_message FROM legacy_purchase_attempts WHERE id = ?",
                    (attempt_id,),
                ).fetchone()
        except Exception:
            return None
        if not row:
            return None
        d = dict(row)
        d["source"] = "legacy"
        d["org_id"] = d.get("customer_name") or d.get("org_name") or "—"
        d["truck"]  = d.get("truck_name") or "—"
        d["total"]  = d.get("order_total") or d.get("truck_price")
        return d
    if source == "roster":
        import sqlite3 as _sqlite
        if not os.path.exists(ROSTER_DB_PATH):
            return None
        try:
            conn = _sqlite.connect(ROSTER_DB_PATH, timeout=5.0)
            conn.row_factory = _sqlite.Row
            row = conn.execute(
                """SELECT pa.id AS attempt_id, pa.status, pa.mode,
                          COALESCE(pa.completed_at, pa.started_at) AS ts,
                          pa.error_message, pa.confirmation_number,
                          pa.order_total, te.truck_title, te.truck_url,
                          te.truck_price, np.org_name
                   FROM purchase_attempts pa
                   LEFT JOIN truck_events te ON te.id = pa.truck_event_id
                   LEFT JOIN nonprofits   np ON np.id = pa.nonprofit_id
                   WHERE pa.id = ?""",
                (attempt_id,),
            ).fetchone()
            conn.close()
        except _sqlite.Error:
            return None
        if not row:
            return None
        d = dict(row)
        d["source"] = "roster"
        d["org_id"] = d.get("org_name") or "—"
        d["truck"]  = d.get("truck_title") or "—"
        d["total"]  = d.get("order_total") or d.get("truck_price")
        return d
    return None


def _load_similar_failures(source: str, attempt_id: int, org_hint: str | None,
                            status_hint: str | None, limit: int = 10) -> list[dict]:
    """Pull up to `limit` recent non-success attempts that look similar.
    Same source as the current row to keep field shapes consistent.

    Heuristic: same org OR same status, last 30 days, exclude self.
    'Similar' isn't precisely defined here — the AI gets to weigh signal.
    """
    rows: list[dict] = []
    if source == "legacy":
        try:
            with get_conn() as c:
                cur = c.execute(
                    """SELECT id AS attempt_id, ts, status, org_name,
                              customer_name, truck_name, truck_url,
                              order_total, error_message, confirmation_number
                       FROM legacy_purchase_attempts
                       WHERE id != ?
                         AND ts >= datetime('now', '-30 day')
                         AND status NOT IN ('SUCCESS', 'success', 'dry_run_ok', 'ok')
                         AND (
                              (? IS NOT NULL AND (org_name = ? OR customer_name = ?))
                           OR (? IS NOT NULL AND status = ?)
                         )
                       ORDER BY ts DESC
                       LIMIT ?""",
                    (attempt_id, org_hint, org_hint, org_hint,
                     status_hint, status_hint, limit),
                )
                for r in cur.fetchall():
                    rows.append({
                        "ts": r["ts"], "status": r["status"],
                        "org_name": r["org_name"] or r["customer_name"],
                        "truck_name": r["truck_name"],
                        "order_total": r["order_total"],
                        "error_message": r["error_message"],
                        "confirmation_number": r["confirmation_number"],
                        "source": "legacy",
                    })
        except Exception:
            pass
    elif source == "roster":
        import sqlite3 as _sqlite
        if not os.path.exists(ROSTER_DB_PATH):
            return []
        try:
            conn = _sqlite.connect(ROSTER_DB_PATH, timeout=5.0)
            conn.row_factory = _sqlite.Row
            cur = conn.execute(
                """SELECT pa.id AS attempt_id, pa.status,
                          COALESCE(pa.completed_at, pa.started_at) AS ts,
                          pa.error_message, pa.order_total,
                          te.truck_title, np.org_name
                   FROM purchase_attempts pa
                   LEFT JOIN truck_events te ON te.id = pa.truck_event_id
                   LEFT JOIN nonprofits   np ON np.id = pa.nonprofit_id
                   WHERE pa.id != ?
                     AND COALESCE(pa.completed_at, pa.started_at) >= datetime('now', '-30 day')
                     AND pa.status NOT IN ('success', 'dry_run_ok')
                     AND (
                          (? IS NOT NULL AND np.org_name = ?)
                       OR (? IS NOT NULL AND pa.status = ?)
                     )
                   ORDER BY COALESCE(pa.completed_at, pa.started_at) DESC
                   LIMIT ?""",
                (attempt_id, org_hint, org_hint, status_hint, status_hint, limit),
            )
            for r in cur.fetchall():
                rows.append({
                    "ts": r["ts"], "status": r["status"],
                    "org_name": r["org_name"],
                    "truck_title": r["truck_title"],
                    "order_total": r["order_total"],
                    "error_message": r["error_message"],
                    "source": "roster",
                })
            conn.close()
        except _sqlite.Error:
            pass
    return rows


@bp.route("/api/admin/purchases/<source>/<int:attempt_id>/diagnose", methods=["GET"])
@auth.login_required
def admin_diagnose_purchase(source: str, attempt_id: int):
    """AI-generated diagnosis for one purchase attempt. Cached per
    (source, attempt_id) in the dashboard.db purchase_diagnoses table so
    re-opens of the same row don't re-bill the Claude API.

    Query params:
      refresh=1  — bypass the cache and force a re-generation. Useful
                   after the operator edits the underlying autobuy code
                   and wants the diagnosis to consider their fix.
    """
    source = (source or "").lower()
    if source not in ("legacy", "roster"):
        return jsonify({"success": False, "error": "source must be roster|legacy"}), 400

    refresh = request.args.get("refresh") in ("1", "true", "yes")

    # Cache lookup (skip on ?refresh=1)
    if not refresh:
        try:
            with get_conn() as c:
                row = c.execute(
                    "SELECT diagnosis, suggested_action, model, similar_count, "
                    "created_at FROM purchase_diagnoses WHERE source = ? AND attempt_id = ?",
                    (source, attempt_id),
                ).fetchone()
            if row:
                return jsonify({
                    "success": True,
                    "cached":  True,
                    "data": {
                        "diagnosis":        row["diagnosis"],
                        "suggested_action": row["suggested_action"] or "",
                        "model":            row["model"],
                        "similar_count":    int(row["similar_count"] or 0),
                        "generated_at":     row["created_at"],
                    },
                })
        except Exception:
            # Cache lookup failure isn't fatal — fall through to live call.
            pass

    purchase = _load_purchase_row(source, attempt_id)
    if not purchase:
        return jsonify({"success": False, "error": "purchase not found"}), 404

    org_hint = purchase.get("org_name") or purchase.get("org_id")
    status_hint = purchase.get("status")
    similar = _load_similar_failures(source, attempt_id, org_hint, status_hint, limit=10)

    from diagnose import diagnose_failure
    result = diagnose_failure(purchase, similar)
    if not result.get("ok"):
        return jsonify({"success": False, "error": result.get("error", "diagnosis failed")}), 502

    # Persist to cache. Best-effort — if the write fails, we still return
    # the live result to the operator.
    try:
        with get_conn() as c:
            c.execute(
                """INSERT OR REPLACE INTO purchase_diagnoses
                   (source, attempt_id, diagnosis, suggested_action, model,
                    similar_count, input_tokens, output_tokens, cache_read_tokens)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source, attempt_id,
                    result["diagnosis"], result.get("suggested_action") or "",
                    result.get("model"),
                    len(similar),
                    result.get("input_tokens", 0),
                    result.get("output_tokens", 0),
                    result.get("cache_read_input_tokens", 0),
                ),
            )
    except Exception:
        pass

    return jsonify({
        "success": True,
        "cached":  False,
        "data": {
            "diagnosis":        result["diagnosis"],
            "suggested_action": result.get("suggested_action") or "",
            "model":            result.get("model"),
            "similar_count":    len(similar),
            "tokens": {
                "input":      result.get("input_tokens", 0),
                "output":     result.get("output_tokens", 0),
                "cache_read": result.get("cache_read_input_tokens", 0),
            },
        },
    })


@bp.route("/api/admin/audit", methods=["GET"])
@auth.super_admin_required
def admin_audit_log():
    """Dashboard's own admin-action audit (separate from purchase audit).

    Query params:
      limit  — cap rows (default 500, max 5000)
      days   — only return entries from the last N days; omit for all-time
    """
    limit = max(1, min(request.args.get("limit", 500, type=int), 5000))
    days = request.args.get("days", type=int)
    where = ""
    params: list = []
    if days and days > 0:
        # admin_audit.ts is stored as 'YYYY-MM-DD HH:MM:SS' (sqlite datetime('now'))
        where = " WHERE ts >= datetime('now', ?)"
        params.append(f"-{days} day")
    params.append(limit)
    with get_conn() as c:
        rows = c.execute(
            f"SELECT * FROM admin_audit{where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        # Distinct user emails for the filter dropdown
        users = c.execute(
            "SELECT DISTINCT user_email FROM admin_audit "
            "WHERE user_email IS NOT NULL ORDER BY user_email"
        ).fetchall()
    return jsonify({
        "success": True,
        "data": [dict(r) for r in rows],
        "users": [u["user_email"] for u in users],
    })


@bp.route("/api/admin/analytics", methods=["GET"])
@auth.login_required
def admin_analytics():
    """Aggregated insights from the data the system actually captures.

    Sources used (and their honest limits):
      - good360_run_log.json: scans + per-scan trucks[]. No org_id / no
        login_ok in this file — those weren't being captured by the monitor.
      - audit JSONL: purchases. No code currently writes purchase events
        (the audit() helper exists but isn't called). Always returns zeros
        until that wiring lands.
      - dashboard.db `notifications`: every outbound Telegram alert, with
        delivery status. Captures only post-rebuild events.
      - dashboard.db `customers`: status + autobuy + cooldown snapshot.

    Range param: ?days=7|30|90|365 (default 30). Truncates each series to
    that window so the response stays small even on a long-lived install.
    """
    from collections import Counter, defaultdict
    days = max(1, min(request.args.get("days", 30, type=int), 365))
    cutoff = datetime.now(UTC) - timedelta(days=days)

    # ---------- scans + trucks ---------------------------------------------
    # Prefer the SQL scans table (no rewrite-amplification, queryable for
    # longer windows) and fall back to the JSON file if SQL is empty.
    def _parse_run_time(s):
        # Stored timestamps may be ISO-8601 ("2026-05-07T15:14:35-04:00") or
        # the older "YYYY-MM-DD HH:MM:SS" format. Treat naive timestamps as
        # UTC — small ET skew is acceptable for analytics windowing.
        if not s:
            return None
        try:
            return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        except ValueError:
            try:
                return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return None

    runs = []
    sql_rows: list = []
    try:
        with get_conn() as _c:
            sql_rows = _c.execute(
                "SELECT ts, alert_sent, action, truck_count, available_count, "
                "trucks_json FROM scans WHERE ts >= ? ORDER BY id ASC",
                (cutoff.isoformat(timespec="seconds"),),
            ).fetchall()
    except Exception:
        sql_rows = []
    if sql_rows:
        for r in sql_rows:
            ts = _parse_run_time(r["ts"])
            if not ts:
                continue
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            try:
                trucks = json.loads(r["trucks_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                trucks = []
            raw = {
                "time":       r["ts"],
                "alert_sent": bool(r["alert_sent"]),
                "action":     r["action"] or "",
                "trucks":     trucks,
            }
            runs.append({"ts": ts, "raw": raw})
    else:
        rl = _safe_json(RUN_LOG)
        raw_runs = rl.get("runs", rl) if isinstance(rl, dict) else (rl or [])
        for r in raw_runs:
            ts = _parse_run_time(r.get("time"))
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            if ts is None or ts < cutoff:
                continue
            runs.append({"ts": ts, "raw": r})

    scan_count = len(runs)
    truck_observations = sum(len(r["raw"].get("trucks") or []) for r in runs)
    availability_events = sum(
        sum(1 for t in (r["raw"].get("trucks") or []) if t.get("available"))
        for r in runs
    )
    alerts_sent = sum(1 for r in runs if r["raw"].get("alert_sent"))

    # Actual span of the data (may be much narrower than the requested window
    # if the monitor is new). The UI uses this to avoid claims like
    # "200 scans over 30 days" when really the 200 scans are concentrated
    # in a 3-hour window today.
    data_first = runs[0]["ts"].isoformat() if runs else None
    data_last = runs[-1]["ts"].isoformat() if runs else None
    data_span_seconds = (
        int((runs[-1]["ts"] - runs[0]["ts"]).total_seconds()) if len(runs) > 1 else 0
    )
    avail_rate_pct = round(availability_events / truck_observations * 100, 2) if truck_observations else 0.0

    scans_per_day = Counter()
    avail_per_day = Counter()
    alerts_per_day = Counter()
    scans_per_hour = Counter()
    truck_observed = Counter()
    truck_available = Counter()

    for r in runs:
        d = r["ts"].strftime("%Y-%m-%d")
        scans_per_day[d] += 1
        scans_per_hour[r["ts"].hour] += 1
        if r["raw"].get("alert_sent"):
            alerts_per_day[d] += 1
        for t in (r["raw"].get("trucks") or []):
            name = (t.get("name") or "unknown").strip()
            truck_observed[name] += 1
            if t.get("available"):
                truck_available[name] += 1
                avail_per_day[d] += 1

    # ---------- log-severity (last N log lines) -----------------------------
    log_lines = _docker_logs("monitor", 1000) or _read_last_lines(CRON_LOG, 1000)
    log_errors = log_warns = log_ok = 0
    for raw in log_lines:
        line = raw.split(" | ", 1)[1] if " | " in raw[:40] else raw
        u = line.upper()
        if "ERROR" in u or "FAIL" in u or "TRACEBACK" in u or "❌" in line:
            log_errors += 1
        elif "WARN" in u or "MISSED" in u or "⚠" in line:
            log_warns += 1
        elif "✅" in line or " SUCCESS" in u or "PURCHASED" in u:
            log_ok += 1

    # ---------- notifications -----------------------------------------------
    notif_per_day_level = defaultdict(lambda: {"info": 0, "warn": 0, "error": 0, "success": 0})
    notif_total = notif_delivered = 0
    with get_conn() as c:
        notifs = c.execute(
            f"SELECT ts, level, delivered FROM notifications "
            f"WHERE ts >= ? ORDER BY ts ASC",
            (cutoff.isoformat(timespec="seconds"),),
        ).fetchall()
        cust_rows = c.execute(
            "SELECT status, in_rotation, cooldown_until FROM customers"
        ).fetchall()
        # Total customer count is independent of the time window.

    for n in notifs:
        ts_str = n["ts"]
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            d = ts.strftime("%Y-%m-%d")
        except (ValueError, AttributeError):
            d = "unknown"
        lvl = (n["level"] or "info").lower()
        if lvl not in notif_per_day_level[d]:
            lvl = "info"
        notif_per_day_level[d][lvl] += 1
        notif_total += 1
        if n["delivered"]:
            notif_delivered += 1

    # ---------- customers summary -------------------------------------------
    cust_by_status = Counter()
    in_rotation = paused = cooling = 0
    now_iso = datetime.now(UTC).isoformat()
    for c_ in cust_rows:
        cust_by_status[c_["status"] or "unknown"] += 1
        if c_["in_rotation"]:
            in_rotation += 1
        else:
            paused += 1
        if c_["cooldown_until"] and c_["cooldown_until"] > now_iso:
            cooling += 1

    # ---------- purchases ---------------------------------------------------
    # Source-of-truth: roster.db.purchase_attempts (joined with truck_events
    # + nonprofits). Populated by autobuy_v2 on every attempt.
    purchases = _roster_purchase_rows(days=days, limit=10_000)
    purch_ok = sum(1 for p in purchases if (p.get("status") or "").lower() in ("success", "dry_run_ok"))
    purch_fail = sum(1 for p in purchases
                     if (p.get("status") or "").lower().startswith("fail")
                        or (p.get("status") or "").lower() in ("error", "missed", "manual_required"))
    purch_spend = sum(float(p.get("order_total") or 0) for p in purchases
                      if (p.get("status") or "").lower() == "success")

    # ---------- shape series for charts -------------------------------------
    # Dense day list so the chart shows zero days as zero, not gaps.
    day_keys = []
    for i in range(days):
        d = (datetime.now(UTC) - timedelta(days=days - 1 - i)).strftime("%Y-%m-%d")
        day_keys.append(d)

    series_scans = [{"date": d, "n": scans_per_day.get(d, 0)} for d in day_keys]
    series_avail = [{"date": d, "n": avail_per_day.get(d, 0)} for d in day_keys]
    series_alerts = [{"date": d, "n": alerts_per_day.get(d, 0)} for d in day_keys]
    series_hour = [{"hour": h, "n": scans_per_hour.get(h, 0)} for h in range(24)]
    series_notifs = [{
        "date": d,
        "info":    notif_per_day_level[d]["info"],
        "warn":    notif_per_day_level[d]["warn"],
        "error":   notif_per_day_level[d]["error"],
        "success": notif_per_day_level[d]["success"],
    } for d in day_keys]

    # Trucks ranked by absolute availability count (more useful than rate
    # alone — a 100% rate on 1 observation is noise).
    trucks_table = []
    for name, observed in truck_observed.most_common(20):
        avail = truck_available.get(name, 0)
        trucks_table.append({
            "name": name,
            "observed": observed,
            "available": avail,
            "rate": round(avail / observed * 100, 1) if observed else 0.0,
        })
    trucks_table.sort(key=lambda r: (r["available"], r["rate"]), reverse=True)

    return jsonify({
        "success": True,
        "data": {
            "range_days": days,
            "data_first": data_first,
            "data_last": data_last,
            "data_span_seconds": data_span_seconds,
            "kpi": {
                "scan_count": scan_count,
                "truck_observations": truck_observations,
                "availability_events": availability_events,
                "availability_rate_pct": avail_rate_pct,
                "alerts_sent": alerts_sent,
                "log_errors": log_errors,
                "log_warns": log_warns,
                "log_ok": log_ok,
                "notifications_total": notif_total,
                "notifications_delivered": notif_delivered,
                "purchases_ok": purch_ok,
                "purchases_fail": purch_fail,
                "purchases_spend": purch_spend,
            },
            "series": {
                "scans_per_day": series_scans,
                "availability_per_day": series_avail,
                "alerts_per_day": series_alerts,
                "scans_per_hour": series_hour,
                "notifications_per_day": series_notifs,
            },
            "trucks": trucks_table,
            "customers": {
                "total": sum(cust_by_status.values()),
                "by_status": dict(cust_by_status),
                "in_rotation": in_rotation,
                "paused": paused,
                "cooling_off": cooling,
            },
            "data_gaps": [
                # Be explicit about what we CAN'T compute, so the UI can
                # surface it instead of pretending the answer is zero.
                "Run log doesn't capture login_ok / org_id, so per-org login "
                "health and per-org scan attribution can't be charted yet.",
                # Note: purchase_attempts in roster.db is now the canonical
                # source for buy attempts/totals. If purchase counts read 0
                # but autobuy fired, check that autobuy_v2 wrote rows there.
            ],
        },
    })


@bp.route("/api/admin/notifications", methods=["GET"])
@auth.login_required
def admin_notifications():
    """Telegram notifications mirror. Every outbound alert recorded by
    notifications_log.record_telegram() shows up here for the operator.

    Query params:
      limit  — max rows (default 200, capped at 1000)
      level  — filter by 'info'|'warn'|'error'|'success' (optional)
      source — filter by 'monitor'|'autobuy'|'watchdog'|'report'|'deadman'
    """
    limit  = max(1, min(request.args.get("limit", 200, type=int), 1000))
    level  = (request.args.get("level") or "").strip().lower()
    source = (request.args.get("source") or "").strip().lower()

    where = []
    params: list = []
    if level in ("info", "warn", "error", "success"):
        where.append("level = ?")
        params.append(level)
    if source:
        where.append("source = ?")
        params.append(source)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)

    with get_conn() as c:
        rows = c.execute(
            f"SELECT id, ts, source, level, channel, title, message, delivered, error "
            f"FROM notifications {where_sql} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        # Summary counters for the dashboard's metric strip.
        summary = c.execute(
            "SELECT level, COUNT(*) AS n FROM notifications "
            "WHERE ts >= datetime('now', '-7 day') GROUP BY level"
        ).fetchall()

    return jsonify({
        "success": True,
        "data": [dict(r) for r in rows],
        "summary": {row["level"]: int(row["n"]) for row in summary},
    })


@bp.route("/api/admin/notifications/<int:notif_id>", methods=["DELETE"])
@auth.super_admin_required
def admin_delete_notification(notif_id: int):
    """Delete a single notification row. Audit-logged."""
    try:
        with get_conn() as c:
            c.execute("DELETE FROM notifications WHERE id = ?", (notif_id,))
            deleted = c.total_changes
    except Exception as e:
        return jsonify({"success": False, "error": f"delete failed: {e}"}), 500
    if not deleted:
        return jsonify({"success": False, "error": "not found"}), 404
    auth.audit("notification.delete", target=str(notif_id),
               detail=f"deleted {deleted} row(s)")
    return jsonify({"success": True, "deleted": deleted})


@bp.route("/api/admin/notifications", methods=["DELETE"])
@auth.super_admin_required
def admin_delete_notifications_bulk():
    """Bulk-delete notifications. Without filters this clears the entire
    table; with `level` or `source` it only deletes matching rows. The
    same filter shape as GET /api/admin/notifications, so the UI can wire
    a single 'Clear filtered' button to whatever filter is active."""
    level  = (request.args.get("level")  or "").strip().lower()
    source = (request.args.get("source") or "").strip().lower()
    confirm = request.args.get("confirm") in ("1", "true", "yes")

    # Require an explicit confirm flag for the unfiltered nuke — protects
    # against accidental "clear all" hits with no filters set.
    if not level and not source and not confirm:
        return jsonify({
            "success": False,
            "error": "bulk delete without filter requires ?confirm=1",
        }), 400

    where = []
    params: list = []
    if level in ("info", "warn", "error", "success"):
        where.append("level = ?")
        params.append(level)
    if source:
        where.append("source = ?")
        params.append(source)
    where_sql = ("WHERE " + " AND ".join(where)) if where else ""

    try:
        with get_conn() as c:
            c.execute(f"DELETE FROM notifications {where_sql}", params)
            deleted = c.total_changes
    except Exception as e:
        return jsonify({"success": False, "error": f"delete failed: {e}"}), 500

    auth.audit("notification.delete_bulk",
               target=f"level={level or '*'};source={source or '*'}",
               detail=f"deleted {deleted} row(s)")
    return jsonify({"success": True, "deleted": deleted})


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


@bp.route("/api/admin/roster/queue", methods=["GET"])
@auth.login_required
def roster_queue():
    """Snapshot of the round-robin: who's next up, who got the last buy, and
    who's currently in cool-off. Mirrors the eligibility logic used by
    quickbeed.list_eligible_customers / select_next_round_robin."""
    with get_conn() as c:
        # Eligible queue. Operator's manual_queue_position wins when set;
        # unranked rows fall back to least-recently-used. Cooldown rows are
        # excluded here (and re-enter at their manual position once cleared).
        # Limit raised so the operator sees the full set they can drag.
        eligible = c.execute(
            """SELECT id, organization_name, full_name, priority_level, max_budget,
                      last_used_at, last_purchase_at, cooldown_until, status, in_rotation,
                      manual_queue_position
                 FROM customers
                WHERE status = 'active'
                  AND in_rotation = 1
                  AND (cooldown_until IS NULL OR cooldown_until < datetime('now'))
                ORDER BY (manual_queue_position IS NULL),
                         manual_queue_position ASC,
                         COALESCE(last_used_at, '1970-01-01') ASC,
                         id ASC
                LIMIT 50"""
        ).fetchall()

        last = c.execute(
            """SELECT id, organization_name, full_name, last_purchase_at, last_used_at
                 FROM customers
                WHERE last_purchase_at IS NOT NULL
                ORDER BY last_purchase_at DESC LIMIT 1"""
        ).fetchone()

        cooldowns = c.execute(
            """SELECT id, organization_name, full_name, cooldown_until, last_used_at,
                      last_purchase_at, status, in_rotation
                 FROM customers
                WHERE cooldown_until IS NOT NULL AND cooldown_until >= datetime('now')
                ORDER BY cooldown_until ASC"""
        ).fetchall()

        # Counts for the small meta line in the UI.
        eligible_total = c.execute(
            """SELECT COUNT(*) AS n FROM customers
                WHERE status = 'active' AND in_rotation = 1
                  AND (cooldown_until IS NULL OR cooldown_until < datetime('now'))"""
        ).fetchone()["n"]
        paused_total = c.execute(
            """SELECT COUNT(*) AS n FROM customers
                WHERE status = 'active' AND in_rotation = 0"""
        ).fetchone()["n"]

    queue = [dict(r) for r in eligible]
    return jsonify({
        "success": True,
        "data": {
            "next": queue[0] if queue else None,
            "queue": queue,
            "last_purchase": dict(last) if last else None,
            "cooldowns": [dict(r) for r in cooldowns],
            "summary": {
                "eligible_total": int(eligible_total),
                "paused_total": int(paused_total),
                "cooldown_total": len(cooldowns),
            },
        },
    })


@bp.route("/api/admin/roster/queue/reorder", methods=["POST"])
@auth.login_required
def roster_queue_reorder():
    """Persist the operator's drag-and-drop order. Body: {"order": [id1, id2, ...]}.

    Ranks listed IDs 0..N-1 in manual_queue_position; rows not in the list are
    cleared (NULL) so they fall back to LRU rotation behind the ranked set.
    Cooldown still filters at selection time — manual order is a hint, not a
    bypass.
    """
    body = request.get_json(silent=True) or {}
    order = body.get("order")
    if not isinstance(order, list):
        return jsonify({"success": False, "error": "order must be a list of customer ids"}), 400
    # Clamp absurd payloads but allow the full visible queue (matches the 50
    # limit in roster_queue) plus headroom.
    if len(order) > 200:
        return jsonify({"success": False, "error": "too many ids"}), 400
    # Stringify defensively — the JS may send numbers; the column is TEXT.
    ids = [str(x) for x in order if x is not None]
    with get_conn() as c:
        # Clear all manual positions first so removed rows fall back to LRU.
        c.execute("UPDATE customers SET manual_queue_position = NULL")
        for pos, cid in enumerate(ids):
            c.execute(
                "UPDATE customers SET manual_queue_position = ? WHERE id = ?",
                (pos, cid),
            )

    # Push the new order into roster.db immediately — the purchase engine
    # reads nonprofits.manual_rank, and waiting for the poll loop would
    # leave up to a 5-minute window where the old order still applies.
    # Best-effort: a sync failure must not fail the reorder itself.
    try:
        import quickbeed_roster_sync
        quickbeed_roster_sync.sync_to_roster()
    except Exception:  # noqa: BLE001
        import traceback; traceback.print_exc()

    return jsonify({"success": True, "data": {"ranked": len(ids)}})


@bp.route("/api/admin/login-attempts", methods=["GET"])
@auth.login_required
def list_login_attempts():
    """Recent Good360 login attempts captured by login_telemetry.
    Used by the Scans page's 'Login health' panel."""
    limit = max(1, min(request.args.get("limit", 50, type=int), 500))
    with get_conn() as c:
        rows = c.execute(
            "SELECT id, ts, source, email, success, duration_ms, error "
            "FROM good360_login_attempts ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        # Summary over the last 24h for the metric strip.
        summary = c.execute(
            "SELECT COUNT(*) AS total, "
            "       SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) AS ok "
            "FROM good360_login_attempts WHERE ts >= datetime('now','-1 day')"
        ).fetchone()
    rows_d = [dict(r) for r in rows]
    last_ok = next((r for r in rows_d if r["success"]), None)
    last_fail = next((r for r in rows_d if not r["success"]), None)
    return jsonify({
        "success": True,
        "data": rows_d,
        "summary": {
            "total_24h": int(summary["total"] or 0),
            "ok_24h":    int(summary["ok"] or 0),
            "last_ok":   last_ok,
            "last_fail": last_fail,
        },
    })


@bp.route("/api/admin/test-runs", methods=["GET"])
@auth.super_admin_required
def list_test_runs():
    """Recent admin-triggered checkout test runs."""
    limit = max(1, min(request.args.get("limit", 200, type=int), 1000))
    with get_conn() as c:
        rows = c.execute(
            "SELECT id, ts, started_at, finished_at, status, customer_name, customer_email, "
            "truck_url, card_brand, card_last4, result_summary, error, screenshot_path "
            "FROM test_runs ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return jsonify({"success": True, "data": [dict(r) for r in rows]})


_CARD_BRAND_RE = [
    ("visa",       re.compile(r"^4")),
    ("mastercard", re.compile(r"^(5[1-5]|2[2-7])")),
    ("amex",       re.compile(r"^(34|37)")),
    ("discover",   re.compile(r"^6(011|5)")),
]


def _classify_card(pan: str) -> str:
    digits = re.sub(r"\D", "", pan or "")
    for brand, rx in _CARD_BRAND_RE:
        if rx.match(digits):
            return brand
    return "unknown"


@bp.route("/api/admin/test-runs", methods=["POST"])
@auth.super_admin_required
def create_test_run():
    """Submit a new checkout test. PAN + CVV are read once for the runner
    and never persisted — only brand + last4 are stored in the audit row."""
    data = request.get_json(silent=True) or {}
    name = (data.get("customer_name") or "").strip()
    email = (data.get("customer_email") or "").strip()
    truck_url = (data.get("truck_url") or "").strip() or None
    pan = re.sub(r"\D", "", (data.get("card_number") or ""))
    expiry = re.sub(r"\D", "", (data.get("card_expiry") or ""))
    cvv = re.sub(r"\D", "", (data.get("card_cvv") or ""))
    # live_submit=true → dry_run=false (agent will click Place Order).
    # Default off so accidental clicks are non-destructive.
    live_submit = bool(data.get("live_submit"))

    errors: list[str] = []
    if not name:  errors.append("customer_name is required")
    if not email or "@" not in email: errors.append("customer_email must be a valid email")
    if not (12 <= len(pan) <= 19): errors.append("card_number must be 12–19 digits")
    if len(expiry) not in (4, 6): errors.append("card_expiry must be MMYY or MMYYYY")
    if not (3 <= len(cvv) <= 4): errors.append("card_cvv must be 3 or 4 digits")
    if errors:
        return jsonify({"success": False, "error": "; ".join(errors)}), 400

    # Rate limit: cap at TEST_RUN_HOURLY_LIMIT runs in the trailing 60 minutes
    # so accidental Run-test mashing can't hammer the master Good360 account.
    # The limit counts every status — queued / running / completed / failed —
    # since each one represents an attempt that touched (or will touch) the
    # account. Default 3/hour; override via env.
    hourly_cap = max(1, int(os.environ.get("TEST_RUN_HOURLY_LIMIT", "3")))
    with get_conn() as c:
        recent = c.execute(
            "SELECT COUNT(*) AS n, MIN(ts) AS earliest "
            "FROM test_runs WHERE ts >= datetime('now', '-1 hour')"
        ).fetchone()
    if (recent["n"] or 0) >= hourly_cap:
        return jsonify({
            "success": False,
            "error": (f"Rate limit: {hourly_cap} test runs per hour. "
                      f"Earliest in the current window started at {recent['earliest']} UTC. "
                      f"Wait until that row falls out of the trailing hour, "
                      f"or raise TEST_RUN_HOURLY_LIMIT in Settings."),
        }), 429

    brand = _classify_card(pan)
    last4 = pan[-4:]
    u = auth.current_user() or {}

    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO test_runs
                 (status, customer_name, customer_email, truck_url,
                  card_brand, card_last4, result_summary, created_by_user_id)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "queued",
                name, email, truck_url, brand, last4,
                "queued — runner will start when the lock is free",
                u.get("id"),
            ),
        )
        new_id = cur.lastrowid
        c.execute(
            "INSERT INTO admin_audit(user_id, user_email, action, target, detail, ip) "
            "VALUES (?,?,?,?,?,?)",
            (u.get("id"), u.get("email"), "test_run.create",
             f"test_run:{new_id}",
             f"customer={name!r} card={brand}/{last4} truck={truck_url or 'auto'}",
             request.remote_addr),
        )
        row = c.execute(
            "SELECT id, ts, status, customer_name, customer_email, truck_url, "
            "card_brand, card_last4, result_summary FROM test_runs WHERE id = ?",
            (new_id,),
        ).fetchone()

    # Spawn the Playwright runner in a background thread. The PAN + CVV are
    # passed by value into the thread closure; they're never persisted and
    # go out of scope when the thread exits.
    try:
        import test_runner  # type: ignore
        t = threading.Thread(
            target=test_runner.run_in_background,
            kwargs=dict(
                test_id=new_id,
                customer_name=name,
                customer_email=email,
                truck_url=truck_url,
                card_number=pan,
                card_expiry=expiry,
                card_cvv=cvv,
                dry_run=not live_submit,
            ),
            daemon=True,
            name=f"test_runner_{new_id}",
        )
        t.start()
    except Exception as exc:  # pragma: no cover — defensive
        with get_conn() as c:
            c.execute(
                "UPDATE test_runs SET status=?, error=? WHERE id=?",
                ("failed", f"failed to spawn runner: {exc}", new_id),
            )

    # Local pan/cvv references go out of scope at function exit.
    del pan, cvv
    return jsonify({"success": True, "data": dict(row)})


@bp.route("/api/admin/test-runs/<int:test_id>", methods=["GET"])
@auth.super_admin_required
def get_test_run(test_id):
    """Full detail for one test run, with the audit trail attached."""
    with get_conn() as c:
        row = c.execute(
            "SELECT id, ts, started_at, finished_at, status, customer_name, customer_email, "
            "truck_url, card_brand, card_last4, result_summary, error, screenshot_path, "
            "created_by_user_id "
            "FROM test_runs WHERE id = ?",
            (test_id,),
        ).fetchone()
        if not row:
            return jsonify({"success": False, "error": "not found"}), 404
        # Audit entries that reference this test, if any.
        events = c.execute(
            "SELECT ts, user_email, action, detail FROM admin_audit "
            "WHERE target = ? ORDER BY id ASC",
            (f"test_run:{test_id}",),
        ).fetchall()
    data = dict(row)
    data["audit"] = [dict(e) for e in events]
    return jsonify({"success": True, "data": data})


@bp.route("/api/admin/test-runs/<int:test_id>/screenshot", methods=["GET"])
@auth.super_admin_required
def get_test_run_screenshot(test_id):
    """Stream the screenshot taken at the end of the run.
    Validates that the recorded path stays within the screenshots dir so a
    crafted DB row can't trick us into reading arbitrary files."""
    with get_conn() as c:
        row = c.execute(
            "SELECT screenshot_path FROM test_runs WHERE id = ?",
            (test_id,),
        ).fetchone()
    if not row or not row["screenshot_path"]:
        return jsonify({"success": False, "error": "no screenshot"}), 404

    workdir = Path(WORKDIR).resolve()
    shots = (workdir / "test_run_screenshots").resolve()
    requested = (workdir / row["screenshot_path"]).resolve()
    try:
        requested.relative_to(shots)
    except ValueError:
        return jsonify({"success": False, "error": "invalid path"}), 400
    if not requested.exists():
        return jsonify({"success": False, "error": "file missing"}), 404
    return Response(requested.read_bytes(), mimetype="image/png",
                    headers={"Cache-Control": "private, max-age=60"})


@bp.route("/api/admin/test-runs/<int:test_id>/diagnose", methods=["GET"])
@auth.login_required
def admin_diagnose_test_run(test_id: int):
    """AI diagnosis for a test_run failure. Reuses the same Claude-via-
    OpenRouter helper that powers the per-purchase diagnose endpoint, but
    feeds it the test_runs row shape instead of a legacy_purchase_attempt
    row. No caching: test runs are operator-triggered diagnostics so the
    cost of one fresh call per click is negligible."""
    with get_conn() as c:
        row = c.execute(
            "SELECT id, ts, status, customer_name, customer_email, truck_url, "
            "card_brand, card_last4, result_summary, error, screenshot_path "
            "FROM test_runs WHERE id = ?",
            (test_id,),
        ).fetchone()
    if not row:
        return jsonify({"success": False, "error": "test run not found"}), 404

    # Adapt the row into the same dict shape the diagnose helper expects.
    failure = {
        "ts":            row["ts"],
        "status":        row["status"],
        "reason":        (row["result_summary"] or "").split(":", 1)[0].strip(),
        "org_id":        row["customer_name"],
        "truck":         row["truck_url"],
        "error_message": row["error"],
        "detail":        row["result_summary"],
        "source":        "test_run",
    }

    # Recent FAILED/MISSED test runs as the "similar history" — same
    # operator, same general failure family. 30-day window.
    with get_conn() as c:
        similar_rows = c.execute(
            "SELECT id, ts, status, customer_name, truck_url, result_summary, error "
            "FROM test_runs "
            "WHERE id != ? AND status IN ('failed', 'completed') "
            "  AND ts >= datetime('now', '-30 day') "
            "ORDER BY ts DESC LIMIT 10",
            (test_id,),
        ).fetchall()
    similar = [{
        "ts":            s["ts"],
        "status":        s["status"],
        "org_name":      s["customer_name"],
        "truck_url":     s["truck_url"],
        "error_message": s["error"],
        "detail":        s["result_summary"],
        "source":        "test_run",
    } for s in similar_rows]

    from diagnose import diagnose_failure
    result = diagnose_failure(failure, similar)
    if not result.get("ok"):
        return jsonify({"success": False, "error": result.get("error")}), 502
    return jsonify({
        "success": True,
        "data": {
            "diagnosis":        result["diagnosis"],
            "suggested_action": result.get("suggested_action") or "",
            "model":            result.get("model"),
            "similar_count":    len(similar),
        },
    })


@bp.route("/api/admin/test-runs/<int:test_id>", methods=["DELETE"])
@auth.super_admin_required
def delete_test_run(test_id):
    u = auth.current_user() or {}
    with get_conn() as c:
        row = c.execute("SELECT id FROM test_runs WHERE id = ?", (test_id,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "not found"}), 404
        c.execute("DELETE FROM test_runs WHERE id = ?", (test_id,))
        c.execute(
            "INSERT INTO admin_audit(user_id, user_email, action, target, detail, ip) "
            "VALUES (?,?,?,?,?,?)",
            (u.get("id"), u.get("email"), "test_run.delete",
             f"test_run:{test_id}", "deleted", request.remote_addr),
        )
    return jsonify({"success": True})


@bp.route("/api/admin/customers/<customer_id>/purchases", methods=["GET"])
@auth.login_required
def customer_purchases(customer_id):
    """Buy history for a single customer. Reads roster.db.purchase_attempts
    joined to truck_events + nonprofits, filtered to rows whose nonprofit
    has quickbeed_customer_id == customer_id."""
    days = request.args.get("days", 90, type=int)
    limit = max(1, min(request.args.get("limit", 200, type=int), 1000))

    with get_conn() as c:
        row = c.execute(
            "SELECT id, organization_name FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
    if not row:
        return jsonify({"success": False, "error": "not found"}), 404

    purchases = _roster_purchase_rows(
        where_sql="np.quickbeed_customer_id = ?",
        params=(customer_id,),
        days=days,
        limit=limit,
    )

    def _is_ok(p):
        return (p.get("status") or "").lower() in ("success", "dry_run_ok")
    def _is_fail(p):
        s = (p.get("status") or "").lower()
        return s.startswith("fail") or s in ("error", "missed", "manual_required")

    ok = sum(1 for p in purchases if _is_ok(p))
    fail = sum(1 for p in purchases if _is_fail(p))
    total_spend = sum(
        float(p.get("total") or 0)
        for p in purchases
        if _is_ok(p)
    )

    return jsonify({
        "success": True,
        "data": purchases[:limit],
        "summary": {"ok": ok, "fail": fail, "total_spend": total_spend, "days": days},
    })


@bp.route("/api/admin/customers/<customer_id>/rotation", methods=["PATCH"])
@auth.super_admin_required
def customer_set_rotation(customer_id):
    """Toggle whether this customer participates in the autobuy round-robin.
    Maps to the local `in_rotation` flag — purchase eligibility is
    `status == 'active' AND in_rotation = 1 AND no active cooldown`.
    """
    data = request.get_json(silent=True) or {}
    if "in_rotation" not in data:
        return jsonify({"success": False, "error": "missing in_rotation"}), 400
    new_val = 1 if bool(data["in_rotation"]) else 0
    u = auth.current_user() or {}

    with get_conn() as c:
        row = c.execute("SELECT id, organization_name, in_rotation FROM customers WHERE id = ?",
                        (customer_id,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "not found"}), 404
        old_val = int(row["in_rotation"] or 0)
        if old_val == new_val:
            updated = c.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
            return jsonify({"success": True, "data": dict(updated), "no_change": True})
        c.execute("UPDATE customers SET in_rotation = ? WHERE id = ?", (new_val, customer_id))
        c.execute(
            "INSERT INTO admin_audit(user_id, user_email, action, target, detail, ip) "
            "VALUES (?,?,?,?,?,?)",
            (
                u.get("id"),
                u.get("email"),
                "customer.rotation",
                f"customer:{customer_id}",
                f"in_rotation {old_val} → {new_val} ({row['organization_name'] or 'unnamed'})",
                request.remote_addr,
            ),
        )
        updated = c.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()

    # Push the change into roster.db NOW — the purchase engine selects from
    # the roster's nonprofits queue, so waiting for the next periodic sync
    # would leave a window where a toggled-out customer can still be bought
    # for. Best-effort: a sync failure must not fail the toggle itself.
    try:
        import quickbeed_roster_sync
        quickbeed_roster_sync.sync_to_roster()
    except Exception:  # noqa: BLE001
        import traceback; traceback.print_exc()

    return jsonify({"success": True, "data": dict(updated)})


@bp.route("/api/admin/customers/<customer_id>/cooldown", methods=["DELETE"])
@auth.super_admin_required
def customer_clear_cooldown(customer_id):
    """Manually graduate a customer out of cooldown. Clears cooldown_until
    so the round-robin can pick them again immediately — the status and
    in_rotation gates still apply. Audited like the rotation toggle."""
    u = auth.current_user() or {}
    with get_conn() as c:
        row = c.execute(
            "SELECT id, organization_name, cooldown_until FROM customers WHERE id = ?",
            (customer_id,)).fetchone()
        if not row:
            return jsonify({"success": False, "error": "not found"}), 404
        if not row["cooldown_until"]:
            updated = c.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()
            return jsonify({"success": True, "data": dict(updated), "no_change": True})
        old = row["cooldown_until"]
        c.execute("UPDATE customers SET cooldown_until = NULL WHERE id = ?", (customer_id,))
        c.execute(
            "INSERT INTO admin_audit(user_id, user_email, action, target, detail, ip) "
            "VALUES (?,?,?,?,?,?)",
            (
                u.get("id"),
                u.get("email"),
                "customer.cooldown_clear",
                f"customer:{customer_id}",
                f"cooldown_until {old} → cleared ({row['organization_name'] or 'unnamed'})",
                request.remote_addr,
            ),
        )
        updated = c.execute("SELECT * FROM customers WHERE id = ?", (customer_id,)).fetchone()

    # The purchase engine keeps its own cooldown in roster.db — graduate the
    # customer there too, or find_next_available_org would still skip them.
    try:
        import quickbeed_roster_sync
        with quickbeed_roster_sync.roster_conn() as rc:
            rc.execute(
                "UPDATE nonprofits SET cooldown_until = NULL, "
                "status = CASE WHEN status = 'cooldown' THEN 'active' ELSE status END "
                "WHERE quickbeed_customer_id = ?", (customer_id,))
            rc.commit()
    except Exception:  # noqa: BLE001
        import traceback; traceback.print_exc()

    return jsonify({"success": True, "data": dict(updated)})


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


@bp.route("/api/admin/live-trucks", methods=["GET"])
@auth.login_required
def admin_live_trucks():
    """Dedup-by-name list of trucks pulled from recent scan rows. Drives
    the truck dropdown in the per-customer Test Buy modal.

    Query params:
      window_minutes  — restrict to scans within the last N minutes (default 120)
      available_only  — when 1, hide trucks the most recent scan marked sold out
                        (default 0 — show everything we've seen so the operator
                        can pick "always available" trucks that the monitor
                        sometimes catches mid-restock as not available)
      limit           — cap result size (default 100)
    """
    window = max(1, min(request.args.get("window_minutes", 120, type=int), 1440))
    limit  = max(1, min(request.args.get("limit", 100, type=int), 500))
    available_only = request.args.get("available_only") in ("1", "true", "yes")

    seen: dict[str, dict] = {}  # truck name → most-recent observation
    try:
        with get_conn() as c:
            rows = c.execute(
                "SELECT ts, trucks_json FROM scans "
                "WHERE ts >= datetime('now', ?, 'localtime') "
                "ORDER BY id DESC LIMIT 500",
                (f"-{window} minutes",),
            ).fetchall()
    except Exception:
        rows = []

    for r in rows:
        try:
            trucks = json.loads(r["trucks_json"] or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        for t in trucks:
            name = (t.get("name") or "").strip()
            url  = (t.get("url")  or "").strip()
            if not name or not url:
                continue
            # Keep the newest observation per truck name. We DON'T filter
            # by `available` here — the monitor sometimes catches a truck
            # mid-restock as not-available and the operator still wants
            # to test it. The UI shows the availability state next to
            # the name so the operator can decide.
            if name not in seen:
                seen[name] = {
                    "name":       name,
                    "url":        url,
                    "tracked":    bool(t.get("tracked")),
                    "available":  bool(t.get("available")),
                    "price":      t.get("price"),
                    "last_seen":  r["ts"],
                }

    out = list(seen.values())
    if available_only:
        out = [t for t in out if t["available"]]
    out.sort(key=lambda x: (not x["available"], x.get("last_seen") or ""), reverse=False)
    # Above: available first (False<True ordering), then by last_seen desc
    # — re-sort last_seen descending within each group.
    avail = sorted([t for t in out if t["available"]], key=lambda x: x.get("last_seen") or "", reverse=True)
    rest  = sorted([t for t in out if not t["available"]], key=lambda x: x.get("last_seen") or "", reverse=True)
    out = (avail + rest)[:limit]

    return jsonify({
        "success": True,
        "data": out,
        "window_minutes": window,
        "available_count": sum(1 for t in out if t["available"]),
        "total_count":     len(out),
    })


@bp.route("/api/admin/customers/<customer_id>/credentials", methods=["GET"])
@auth.super_admin_required
def customer_credentials(customer_id: str):
    """Return the customer's raw Good360 partner credentials (username +
    password). Super-admin only. Every call audit-logs both locally
    (admin_audit) and upstream on QuickBeed (reason= is forwarded).

    Used by the eye-toggle on the customer detail page so operators can
    visually confirm the credentials we'll use to log into Good360 as
    this customer. NOT to be confused with the safe-projection
    /customers/<id>/live endpoint, which only returns password_length.
    """
    reason = request.args.get("reason") or quickbeed.REASON_CREDENTIAL_USE
    try:
        rec = quickbeed.fetch_full(customer_id, reason=reason)
    except quickbeed.QuickBeedHTTPError as exc:
        return jsonify({"success": False, "error": str(exc), "status": exc.status}), 502
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    pc = rec.get("partner_credentials") or {}
    username = pc.get("username") or ""
    password = pc.get("password") or ""

    auth.audit("customer_credentials_view",
               target=customer_id,
               detail=f"reason={reason}; username_present={bool(username)}; password_present={bool(password)}")

    return jsonify({
        "success": True,
        "data": {
            "username": username,
            "password": password,
            "_reason_logged": reason,
        },
    })


@bp.route("/api/admin/customers/<customer_id>/card-details", methods=["GET"])
@auth.super_admin_required
def customer_card_details(customer_id: str):
    """Return the customer's FULL card details (number, expiry, CVV, billing).
    Super-admin only. Mirrors /credentials: every call audit-logs both
    locally (admin_audit) and upstream on QuickBeed via the forwarded
    reason. Nothing is stored locally — fetched, shown, discarded."""
    reason = request.args.get("reason") or quickbeed.REASON_SUPPORT
    try:
        rec = quickbeed.fetch_full(customer_id, reason=reason)
    except quickbeed.QuickBeedHTTPError as exc:
        return jsonify({"success": False, "error": str(exc), "status": exc.status}), 502
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    cards = []
    for pm in rec.get("payment_methods") or []:
        cards.append({
            "rank": pm.get("rank"),
            "network": pm.get("card_network"),
            "type": pm.get("type"),
            "name_on_card": pm.get("name_on_card"),
            "card_number": pm.get("card_number"),
            "cvv": pm.get("cvv"),
            "exp_month": pm.get("exp_month"),
            "exp_year": pm.get("exp_year"),
            "expiry_normalized": customer_readiness.expiry_mmyy(
                pm.get("exp_month"), pm.get("exp_year")),
            "billing_address": pm.get("billing_address"),
        })
    validation = customer_readiness.validate_record(rec)

    auth.audit("customer_card_view", target=customer_id,
               detail=f"reason={reason}; cards={len(cards)}")

    return jsonify({"success": True, "data": {
        "cards": cards,
        "validation": validation,
        "_reason_logged": reason,
    }})


@bp.route("/api/admin/customers/<customer_id>/revalidate", methods=["POST"])
@auth.login_required
def customer_revalidate(customer_id: str):
    """Resync this one customer from QuickBeed NOW and re-run the
    payment-data validation. fetch_full() refreshes the local mirror,
    flags, and card summary as a side effect; the response tells the
    operator whether a purchase would succeed with the data on file."""
    try:
        rec = quickbeed.fetch_full(customer_id, reason=quickbeed.REASON_RECONCILIATION)
    except quickbeed.QuickBeedHTTPError as exc:
        return jsonify({"success": False, "error": str(exc), "status": exc.status}), 502
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    validation = customer_readiness.validate_record(rec)
    auth.audit("customer_revalidate", target=customer_id,
               detail=f"ok={validation['ok']} blockers={len(validation['blockers'])}")
    return jsonify({"success": True, "validation": validation,
                    "cards_meta": customer_readiness.cards_meta(rec)})


def _build_test_card(card_choice: str, customer_cards: list[dict]) -> tuple[dict | None, str]:
    """Translate `card_choice` from the UI into a runtime card dict.

    Returns (card_dict, label). card_dict is None when the choice is
    invalid (caller surfaces the error). Sandbox is the safe default for
    testing; primary/fallback fetch the customer's real PAN from
    QuickBeed at request time.
    """
    if card_choice == "sandbox":
        return {
            "name":    os.environ.get("SANDBOX_CARD_NAME") or "Sandbox Tester",
            "number":  os.environ.get("SANDBOX_CARD_NUMBER") or "4242424242424242",
            "expiry":  os.environ.get("SANDBOX_CARD_EXPIRY") or "1230",
            "cvv":     os.environ.get("SANDBOX_CARD_CVV") or "123",
            "type":    os.environ.get("SANDBOX_CARD_TYPE") or "visa",
        }, "sandbox card (4242 4242 4242 4242)"

    cards = customer_cards or []
    if card_choice == "primary":
        match = next((c for c in cards if c.get("rank") == "primary"), None) or (cards[0] if cards else None)
        if not match:
            return None, "customer has no payment methods on file"
        return _card_to_org(match), f"primary {match.get('card_network') or 'card'} ****{(match.get('card_number') or '')[-4:]}"
    if card_choice.startswith("fallback:"):
        try:
            idx = int(card_choice.split(":", 1)[1])
        except ValueError:
            return None, "bad fallback index"
        non_primary = [c for c in cards if c.get("rank") != "primary"]
        if idx < 0 or idx >= len(non_primary):
            return None, f"fallback #{idx + 1} not found"
        match = non_primary[idx]
        return _card_to_org(match), f"fallback #{idx + 1} {match.get('card_network') or 'card'} ****{(match.get('card_number') or '')[-4:]}"
    return None, f"unknown card_choice {card_choice!r}"


def _run_test_purchase_via_daemon(test_id: int, customer_id: str,
                                  org_config: dict,
                                  truck_name: str, truck_url: str) -> None:
    """Background worker: posts to the daemon's /test_checkout and updates
    the test_runs row with the outcome. Best-effort capture of the capture
    JSON path so the dashboard can link to it.

    org_key is keyed on the customer (not the test_id) so the daemon
    reuses the same persistent browser context across test buys for one
    customer. That means: login cookie persists, subsequent runs skip
    the cold login round-trip, and the AI gets to see consistent
    behaviour across attempts.
    """
    import urllib.error, urllib.request
    daemon_url = (os.environ.get("DAEMON_URL", "http://daemon:5002").rstrip("/")
                  + "/test_checkout")
    payload = json.dumps({
        "org_key":    f"customer_{customer_id}",
        "org_config": org_config,
        "truck_name": truck_name,
        "truck_url":  truck_url,
    }).encode("utf-8")
    try:
        with get_conn() as c:
            c.execute(
                "UPDATE test_runs SET status=?, started_at=datetime('now'), "
                "result_summary=? WHERE id=?",
                ("running", "calling daemon /test_checkout…", test_id),
            )
    except Exception:
        pass

    try:
        req = urllib.request.Request(
            daemon_url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=300) as r:
            resp = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = (e.read() or b"").decode("utf-8", errors="replace")[:500]
        resp = {"status": "FAILED", "message": f"daemon HTTP {e.code}: {body}"}
    except Exception as e:
        resp = {"status": "FAILED", "message": f"daemon call failed: {type(e).__name__}: {e}"}

    status      = (resp.get("status") or "FAILED").upper()
    message     = resp.get("message") or ""
    capture     = resp.get("capture_path") or None
    # Map daemon statuses to test_runs.status (completed/failed) per the
    # same convention the AI-agent runner uses.
    # CARD_DECLINED is a "completed" run with a card-rejection message —
    # the autobuy flow ran end-to-end and reached the payment processor.
    # That's a valid sandbox-test outcome, not an autobuy bug.
    final_status = "completed" if status in ("SUCCESS", "MISSED", "MANUAL",
                                              "CARD_DECLINED") else \
                   "failed"    if status in ("FAILED", "BLOCKED") else \
                   "completed"  # unknown → record as completed so the operator sees the message
    try:
        with get_conn() as c:
            c.execute(
                "UPDATE test_runs SET status=?, finished_at=datetime('now'), "
                "result_summary=?, error=?, screenshot_path=COALESCE(?, screenshot_path) WHERE id=?",
                (final_status, f"{status}: {message[:400]}",
                 message if final_status == "failed" else None,
                 capture, test_id),
            )
    except Exception:
        pass


@bp.route("/api/admin/customers/<customer_id>/test-purchase", methods=["POST"])
@auth.super_admin_required
def customer_test_purchase(customer_id: str):
    """Operator-triggered test buy for a specific customer + truck + card.

    Body:
      truck_url   — required; the Good360 truck listing URL
      truck_name  — optional; for the cart-contents assertion + alerts
      card_choice — "sandbox" | "primary" | "fallback:N" (default sandbox)

    Returns immediately with a test_runs id so the UI can poll. The
    actual checkout runs in a background thread and posts to the
    daemon's /test_checkout endpoint with an inline org_config built
    from this customer's QuickBeed partner credentials + selected card.
    """
    body = request.get_json(silent=True) or {}
    truck_url   = (body.get("truck_url")   or "").strip()
    truck_name  = (body.get("truck_name")  or "(test buy)").strip()
    card_choice = (body.get("card_choice") or "sandbox").strip()

    if not truck_url:
        return jsonify({"success": False, "error": "truck_url is required"}), 400

    # 1. Pull this customer's partner credentials + cards fresh.
    try:
        rec = quickbeed.fetch_full(customer_id, reason=quickbeed.REASON_CREDENTIAL_USE)
    except quickbeed.QuickBeedHTTPError as exc:
        return jsonify({"success": False, "error": f"QuickBeed: {exc}"}), 502
    except Exception as exc:  # noqa: BLE001
        import traceback; traceback.print_exc()
        return jsonify({"success": False, "error": f"{type(exc).__name__}: {exc}"}), 500

    pc      = rec.get("partner_credentials") or {}
    cards   = rec.get("payment_methods") or []
    profile = rec.get("profile") or {}
    ops     = rec.get("operations") or {}

    email = (pc.get("username") or "").strip()
    password = pc.get("password") or ""
    if not email or not password:
        return jsonify({"success": False,
                        "error": "customer has no partner credentials"}), 400

    # 2. Translate card_choice → runtime card dict.
    card, card_label = _build_test_card(card_choice, cards)
    if card is None:
        return jsonify({"success": False, "error": card_label}), 400

    # 3. Rate-limit on hourly cap. Bumped to 100 by default (was 3) so
    # active operator testing isn't blocked; set TEST_RUN_HOURLY_LIMIT=0
    # in Settings to disable the cap entirely.
    try:
        hourly_cap = int(os.environ.get("TEST_RUN_HOURLY_LIMIT", "100"))
    except ValueError:
        hourly_cap = 100
    if hourly_cap > 0:
        with get_conn() as c:
            recent = c.execute(
                "SELECT COUNT(*) AS n, MIN(ts) AS earliest "
                "FROM test_runs WHERE ts >= datetime('now', '-1 hour')"
            ).fetchone()
        if (recent["n"] or 0) >= hourly_cap:
            return jsonify({
                "success": False,
                "error": (f"Rate limit: {hourly_cap} test runs per hour. "
                          f"Wait until {recent['earliest']} UTC falls out of the "
                          f"trailing hour, set TEST_RUN_HOURLY_LIMIT=0 to disable."),
            }), 429

    # 4. Build the org_config the daemon will use to log in + check out.
    # Pull billing-address fields from the *primary* card's billing
    # address — that's the only customer-sourced billing data we have.
    # If missing, the daemon will fail at the checkout step rather than
    # invent values: per feedback, form-fill data must come from the
    # customer record (no hardcoded defaults).
    primary_card = next((c for c in cards if c.get("rank") == "primary"), None) \
                   or (cards[0] if cards else None)
    primary_billing = (primary_card or {}).get("billing_address") or {}
    name_on_card = (primary_card or {}).get("name_on_card") or profile.get("full_name") or ""
    name_parts = name_on_card.split()
    billing_first = name_parts[0] if name_parts else ""
    billing_last  = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""

    org_config = {
        "name":              profile.get("organization_name") or customer_id,
        "good360_email":     email,
        "good360_password":  password,
        "card":              card,
        "warehouse_address": ops.get("warehouse_address") or "",
        "contact_name":      profile.get("full_name") or "",
        "contact_phone":     profile.get("phone") or "",
        "billing_address":   {
            "firstname": billing_first,
            "lastname":  billing_last,
            "street":    primary_billing.get("street") or "",
            "city":      primary_billing.get("city") or "",
            "state":     primary_billing.get("state") or "",
            "postcode":  primary_billing.get("zip") or "",
            "telephone": profile.get("phone") or "",
            "country":   primary_billing.get("country") or "US",
        },
        "checkout_answers":  {
            "people_helped":       str(ops.get("people_served") or ""),
            "distribution_method": ops.get("distribution_method") or "",
            "warehouse_address":   ops.get("warehouse_address") or "",
            "dock_pallet":         "Yes, we have a dock" if ops.get("has_loading_dock") else "No dock",
        },
        "max_price":         ops.get("max_budget"),
    }

    customer_name  = profile.get("full_name") or profile.get("organization_name") or "(unknown)"
    customer_email = profile.get("email") or ""
    u = auth.current_user() or {}

    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO test_runs (status, customer_name, customer_email, truck_url,
                  card_brand, card_last4, result_summary, created_by_user_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            ("queued", customer_name, customer_email, truck_url,
             (card.get("type") or "")[:20],
             ((card.get("number") or "")[-4:] or "????"),
             # Surface the login email so the operator can verify at a
             # glance that the customer's own Good360 credentials are
             # being used (not a shared master account).
             f"queued — login_as={email} card={card_label}",
             u.get("id")),
        )
        test_id = cur.lastrowid
        c.execute(
            "INSERT INTO admin_audit(user_id, user_email, action, target, detail, ip) "
            "VALUES (?,?,?,?,?,?)",
            (u.get("id"), u.get("email"), "customer.test_purchase",
             f"test_run:{test_id};customer:{customer_id}",
             f"customer={customer_id} login_as={email} card={card_label} truck={truck_url}",
             request.remote_addr),
        )

    # 5. Spawn the daemon-call thread. Don't block the HTTP response —
    # the checkout takes 30–90s and we want the modal to render its
    # loading state immediately.
    t = threading.Thread(
        target=_run_test_purchase_via_daemon,
        args=(test_id, customer_id, org_config, truck_name, truck_url),
        name=f"test_purchase_{test_id}",
        daemon=True,
    )
    t.start()

    # Scrub local card vars so the response object can't leak them.
    org_config["card"] = "***"; card = None
    return jsonify({
        "success": True,
        "data": {
            "test_id":     test_id,
            "card_label":  card_label,
            "customer_id": customer_id,
        },
    })


# ============================================================
# Readiness check — runs the production autobuy code path (autobuy_v2) against
# one customer with a known-decline fake card. This is the ONLY test entry
# point that exercises the same code that real autobuy uses, so a passing
# readiness check for customer X is empirical proof that production autobuy
# will work for customer X (modulo the final card-decline which we expect).
# ============================================================

# 4000 0000 0000 0002 is the universal "card declined" PAN; real payment
# processors return card_declined for it. Hard-coded here on purpose so
# nothing else in the system can substitute a real PAN by accident.
_READINESS_FAKE_CARD = {
    "card_holder_name": "Readiness Test",
    "card_number":      "4000000000000002",
    "card_last4":       "0002",
    "card_expiry_month": 12,
    "card_expiry_year":  2030,
    "card_cvv":         "999",
    "billing_zip":      "30046",
    "card_type":        "visa",
}


def _classify_readiness_stage(success: bool, status: str, error_message: str) -> str:
    """Heuristic: map autobuy_v2's CheckoutResult onto the stage of the
    checkout flow where the run ended. Order matters: credentials and login
    failures often *mention* downstream stages ("blocks Add to Cart, etc.")
    so we must classify them first."""
    if success:
        return "order_confirmed"
    msg = (error_message or "").lower()
    if "declined" in msg or "card was rejected" in msg or "payment failed" in msg \
            or "payment was declined" in msg:
        return "card_declined"
    if "no usable payment_methods" in msg or "no partner credentials" in msg \
            or "no good360 credentials" in msg or "credentials are rejected" in msg \
            or "could not sign in" in msg:
        return "credentials_missing"
    if ("login did not complete" in msg or "login failed" in msg or "sign-in" in msg
            or "sign in" in msg or "log in" in msg
            or "not authorized to log in" in msg or "valid email address" in msg
            or "account sign-in was incorrect" in msg):
        return "login"
    if "place order" in msg or "payment method step" in msg or "credit card details" in msg:
        return "place_order_focus"
    if "card field" in msg or "payment field" in msg or "payment step did not load" in msg \
            or "billing or card fields" in msg:
        return "payment_form"
    if "shipping address" in msg or "warehouse address" in msg:
        return "shipping_address"
    if "checkout question" in msg or "answer questions" in msg:
        return "checkout_questions"
    if ("out of stock" in msg or "checkout is disabled" in msg or "quote" in msg
            or "could not add" in msg or "add to cart" in msg or "could not locate" in msg
            or "full truckload" in msg or "truckload/general-products" in msg
            or "existing cart" in msg or "cart cleanup" in msg
            or "cannot be added" in msg or "disabled" in msg):
        return "add_to_cart"
    return "unknown"


def _run_readiness_check_in_background(test_id: int, org_id: int,
                                       truck_url: str, truck_name: str) -> None:
    """Worker: imports autobuy_v2, runs attempt_purchase with the fake card
    override, writes the result onto the test_runs row."""
    import sys as _sys, traceback as _tb
    # Repo root is already mounted at /root/good-360-bids in this container
    # (see docker-compose missioncontrol volumes). Add to sys.path so we
    # pick up the latest edits to autobuy_v2 without needing a rebuild.
    for _p in ("/root/good-360-bids", "/root/good-360-bids/good360_roster"):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)

    try:
        from good360_autobuy_v2 import attempt_purchase  # type: ignore
        import roster_orchestrator as _ro  # type: ignore
    except Exception as exc:
        _set_test_run_result(test_id, status="failed",
                            summary=f"import failed: {type(exc).__name__}: {exc}",
                            error=_tb.format_exc())
        return

    _set_test_run_result(test_id, status="running",
                        summary=f"logging truck_event for org {org_id}…")
    try:
        event_id = _ro.log_truck_event(
            truck_title=truck_name or "(readiness check)",
            truck_url=truck_url,
            truck_price=0.0,
            truck_location="",
            truck_category="other",   # avoid category-exclusion gates
            raw_data={"_readiness_check": True, "_fake_card_pan_last4": "0002"},
        )
    except Exception as exc:
        _set_test_run_result(test_id, status="failed",
                            summary=f"log_truck_event failed: {type(exc).__name__}: {exc}",
                            error=_tb.format_exc())
        return

    _set_test_run_result(test_id, status="running",
                        summary=f"running autobuy_v2.attempt_purchase(org={org_id}, event={event_id})…")
    try:
        result = attempt_purchase(org_id, event_id,
                                  test_card_override=_READINESS_FAKE_CARD)
    except Exception as exc:
        _set_test_run_result(test_id, status="failed",
                            summary=f"attempt_purchase crashed: {type(exc).__name__}: {exc}",
                            error=_tb.format_exc())
        return

    stage = _classify_readiness_stage(result.success, result.status, result.error_message or "")
    summary = f"stage={stage} · status={result.status}"
    if result.confirmation_number:
        summary += f" · conf={result.confirmation_number}"
    _set_test_run_result(
        test_id,
        status="completed",
        summary=summary[:500],
        error=(result.error_message or None) if not result.success else None,
    )


def _set_test_run_result(test_id: int, *, status: str, summary: str,
                         error: str | None = None) -> None:
    """Best-effort UPDATE on the test_runs row for the readiness worker."""
    try:
        with get_conn() as c:
            if status == "running":
                c.execute(
                    "UPDATE test_runs SET status=?, result_summary=? WHERE id=?",
                    (status, summary[:500], test_id),
                )
            else:
                c.execute(
                    "UPDATE test_runs SET status=?, finished_at=datetime('now'), "
                    "result_summary=?, error=? WHERE id=?",
                    (status, summary[:500], error, test_id),
                )
    except Exception:
        pass


@bp.route("/api/admin/autobuy-readiness-check", methods=["POST"])
@auth.super_admin_required
def autobuy_readiness_check():
    """Run the production autobuy code path against one customer with a
    fake (decline-only) card. This is the unified test entry point: it
    exercises the same autobuy_v2 → MCP-agent stack that real autobuy
    uses, with only the card swapped — so a passing run for org X means
    production autobuy will work for org X.

    Body:
      org_id     — required; roster.db nonprofits.id (not the QuickBeed UUID)
      truck_url  — required; live Good360 truck listing URL
      truck_name — optional; defaults to "(readiness check)"

    Side effects suppressed by the test_card_override path inside
    autobuy_v2: no cooldown, no finding-fee billing, no operator
    notification. Real autobuy state stays untouched.

    Returns:
      {test_id, org_id} immediately. Poll /api/admin/test-runs/<test_id>
      for the running/completed status. Expected runtime per call: 2–6
      minutes depending on Good360's response time at each step.
    """
    body = request.get_json(silent=True) or {}
    truck_url  = (body.get("truck_url")  or "").strip()
    truck_name = (body.get("truck_name") or "(readiness check)").strip()
    try:
        org_id = int(body.get("org_id"))
    except (TypeError, ValueError):
        return jsonify({"success": False, "error": "org_id is required and must be an integer"}), 400
    if not truck_url:
        return jsonify({"success": False, "error": "truck_url is required"}), 400

    # Resolve org_name for the test_runs row (so the UI can show it
    # without joining roster.db on every poll).
    import sys as _sys
    for _p in ("/root/good-360-bids", "/root/good-360-bids/good360_roster"):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    try:
        from roster_orchestrator import get_db_connection as _roster_conn
        with _roster_conn() as rc:
            row = rc.execute("SELECT org_name FROM nonprofits WHERE id=?", (org_id,)).fetchone()
        org_name = row[0] if row else f"org_id={org_id}"
    except Exception as exc:
        return jsonify({"success": False, "error": f"roster lookup failed: {exc}"}), 500
    if not row:
        return jsonify({"success": False, "error": f"no nonprofit with id={org_id} in roster.db"}), 404

    u = auth.current_user() or {}
    with get_conn() as c:
        cur = c.execute(
            """INSERT INTO test_runs (status, customer_name, customer_email, truck_url,
                  card_brand, card_last4, result_summary, created_by_user_id)
               VALUES ('queued', ?, '', ?, 'visa', '0002', ?, ?)""",
            (org_name, truck_url,
             f"queued · autobuy-readiness · org_id={org_id} · fake card ****0002",
             u.get("id")),
        )
        test_id = cur.lastrowid
        c.execute(
            "INSERT INTO admin_audit(user_id, user_email, action, target, detail, ip) "
            "VALUES (?,?,?,?,?,?)",
            (u.get("id"), u.get("email"), "autobuy.readiness_check",
             f"test_run:{test_id};org_id:{org_id}",
             f"org_id={org_id} truck={truck_url}", request.remote_addr),
        )

    t = threading.Thread(
        target=_run_readiness_check_in_background,
        args=(test_id, org_id, truck_url, truck_name),
        name=f"readiness_check_{test_id}",
        daemon=True,
    )
    t.start()

    return jsonify({"success": True,
                    "data": {"test_id": test_id, "org_id": org_id, "org_name": org_name}})


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


@bp.route("/api/admin/data-issues", methods=["GET"])
@auth.login_required
def data_issues_list():
    """Customers with data-readiness flags. data_ok: NULL = never checked,
    0 = flagged (autobuy would fail), 1 = complete."""
    with get_conn() as c:
        rows = c.execute(
            """SELECT id, organization_name, full_name, email, status, in_rotation,
                      data_ok, data_issues, data_checked_at
                 FROM customers
                WHERE status NOT IN ('inactive', 'suspended')
                ORDER BY (data_ok IS NOT 0), organization_name COLLATE NOCASE"""
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["data_issues"] = json.loads(d["data_issues"]) if d["data_issues"] else None
        except (ValueError, TypeError):
            pass
        out.append(d)
    return jsonify({
        "success": True,
        "data": out,
        "flagged_count": sum(1 for d in out if d["data_ok"] == 0),
        "unchecked_count": sum(1 for d in out if d["data_ok"] is None),
        "last_sweep_at": quickbeed.get_sync_state(customer_readiness.SWEEP_STATE_KEY),
    })


@bp.route("/api/admin/data-issues/check", methods=["POST"])
@auth.login_required
def data_issues_check():
    """Run a readiness sweep now: fetch + validate every active/onboarding
    customer, flag incomplete records, alert the operator on new blockers."""
    try:
        summary = customer_readiness.sweep_all()
        auth.audit("customer_readiness_sweep", target="all",
                   detail=f"checked={summary['checked']} flagged={len(summary['flagged'])} "
                          f"errors={len(summary['errors'])}")
        return jsonify({"success": True, **summary})
    except quickbeed.QuickBeedConfigError as exc:
        return jsonify({"success": False, "error": f"config: {exc}"}), 400
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

    # billing_address sourced from the primary card on file. Per
    # [[feedback-customer-data-only]] all form-fill data must come
    # from the customer record — never hardcoded fallbacks.
    primary_billing = (primary or {}).get("billing_address") or {}
    name_on_card = (primary or {}).get("name_on_card") or profile.get("full_name") or ""
    name_parts = name_on_card.split()

    org_config = {
        "quickbeed_customer_id": rec.get("id"),
        "name": profile.get("organization_name"),
        # .strip(): a trailing space in the stored username made Good360's
        # sign-in form reject the email as invalid (reviving homes,
        # 2026-06-12). Whitespace can never be part of an email address, so
        # trimming is safe normalization, not data invention.
        "good360_email": (pc.get("username") or "").strip() or None,
        "good360_password": pc.get("password"),
        "warehouse_address": ops.get("warehouse_address"),
        "has_dock": bool(ops.get("has_loading_dock")),
        "max_price": ops.get("max_budget"),
        "auto_buy_targets": [s.strip() for s in (ops.get("truck_selection") or "").split(",") if s.strip()],
        "card": _card_to_org(primary) if primary else None,
        "fallback_cards": [_card_to_org(c) for c in cards if c is not primary],
        "contact_name":  profile.get("full_name") or "",
        "contact_phone": profile.get("phone") or "",
        "billing_address": {
            "firstname": name_parts[0] if name_parts else "",
            "lastname":  " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
            "street":    primary_billing.get("street") or "",
            "city":      primary_billing.get("city") or "",
            "state":     primary_billing.get("state") or "",
            "postcode":  primary_billing.get("zip") or "",
            "telephone": profile.get("phone") or "",
            "country":   primary_billing.get("country") or "US",
        },
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
    # Expiry normalization lives in customer_readiness so validation and
    # the live org-config translation can never drift apart.
    expiry_mmyy = customer_readiness.expiry_mmyy(pm.get("exp_month"), pm.get("exp_year"))
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
