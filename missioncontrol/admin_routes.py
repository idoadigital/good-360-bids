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


# ============================================================
# Scans, purchases, audit (read-only views)
# ============================================================

def _read_scans(limit: int) -> list[dict]:
    """Return the most recent scans, preferring the SQL `scans` table.
    Falls back to good360_run_log.json if SQL is empty (e.g., immediately
    after migration before the monitor's next scan writes a row)."""
    rows: list[dict] = []
    try:
        with get_conn() as c:
            sql_rows = c.execute(
                "SELECT ts, alert_sent, action, truck_count, available_count, "
                "trucks_json FROM scans ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        for r in sql_rows:
            try:
                trucks = json.loads(r["trucks_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                trucks = []
            rows.append({
                "time":            r["ts"],
                "alert_sent":      bool(r["alert_sent"]),
                "action":          r["action"] or "",
                "truck_count":     r["truck_count"],
                "available_count": r["available_count"],
                "trucks":          trucks,
                # legacy aliases for the existing JS:
                "title":  trucks[0]["name"] if trucks else None,
                "status": "scanned",
            })
    except Exception:
        rows = []
    if rows:
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
    return out


@bp.route("/api/admin/scans", methods=["GET"])
@auth.login_required
def admin_scans():
    """Recent scan activity. Reads from the SQL scans table (preferred)
    or the legacy good360_run_log.json file as a transitional fallback."""
    limit = request.args.get("limit", 100, type=int)
    heartbeat = _safe_json(HEARTBEAT)
    scans = _read_scans(limit)

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


ROSTER_DB_PATH = os.environ.get(
    "ROSTER_DB_PATH",
    "/app/good360_roster/db/roster.db",
)


def _roster_purchase_rows(where_sql: str = "", params: tuple = (), limit: int = 200,
                          days: int | None = None) -> list[dict]:
    """Read joined purchase_attempts + truck_events + nonprofits rows from
    roster.db. Returns rows shaped to the existing /api/admin/purchases
    contract (ts, org_id, truck, total, status, detail) so the UI doesn't
    care that the source switched from JSONL to SQL.

    `where_sql` lets callers filter (e.g. by quickbeed_customer_id). It must
    start with "AND" if non-empty. Empty rows are returned as-is when
    roster.db is unreachable so the UI can render an empty state instead
    of a 500."""
    import sqlite3 as _sqlite
    if not os.path.exists(ROSTER_DB_PATH):
        return []
    where_parts = ["1=1"]
    if days and days > 0:
        where_parts.append("pa.started_at >= datetime('now', ?)")
        params = (*params, f"-{days} day")
    if where_sql:
        where_parts.append(where_sql)
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


@bp.route("/api/admin/purchases", methods=["GET"])
@auth.login_required
def admin_purchases():
    """Purchase attempts: pass + fail.

    Reads from `purchase_attempts` in roster.db (the source-of-truth that
    autobuy_v2 writes to on every attempt), joining truck_events for the
    truck title and nonprofits for the org name. Returns rows in the same
    shape the dashboard's older JSONL-backed view used, so existing JS
    keeps rendering without a rewrite."""
    limit = max(1, min(request.args.get("limit", 200, type=int), 1000))
    days = request.args.get("days", 14, type=int)
    rows = _roster_purchase_rows(limit=limit, days=days)
    return jsonify({"success": True, "data": rows})


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
        # Eligible queue, least-recently-used first (matches quickbeed.py).
        eligible = c.execute(
            """SELECT id, organization_name, full_name, priority_level, max_budget,
                      last_used_at, last_purchase_at, cooldown_until, status, in_rotation
                 FROM customers
                WHERE status = 'active'
                  AND in_rotation = 1
                  AND (cooldown_until IS NULL OR cooldown_until < datetime('now'))
                ORDER BY COALESCE(last_used_at, '1970-01-01') ASC, id ASC
                LIMIT 5"""
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
