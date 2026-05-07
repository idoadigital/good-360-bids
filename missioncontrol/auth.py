"""Authentication: password hashing, session tokens, decorators."""
from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

UTC = timezone.utc
from functools import wraps
from typing import Optional

import bcrypt
from flask import jsonify, redirect, request

from db import get_conn

SESSION_TTL_HOURS = 12
SESSION_COOKIE = "mc_session"

# Login rate limit: max failures per IP per window
LOGIN_FAIL_WINDOW_MIN = 15
LOGIN_FAIL_MAX = 5


def hash_password(plain: str) -> str:
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_user(email: str, password: str, role: str, created_by: Optional[int] = None) -> int:
    if role not in ("super_admin", "admin"):
        raise ValueError("role must be super_admin or admin")
    if not password or len(password) < 12:
        raise ValueError("password must be at least 12 characters")
    h = hash_password(password)
    with get_conn() as c:
        cur = c.execute(
            "INSERT INTO users(email, password_hash, role, created_by) VALUES (?,?,?,?)",
            (email.lower().strip(), h, role, created_by),
        )
        return int(cur.lastrowid)


def authenticate(email: str, password: str) -> Optional[dict]:
    with get_conn() as c:
        row = c.execute(
            "SELECT id, email, password_hash, role FROM users WHERE email = ?",
            (email.lower().strip(),),
        ).fetchone()
    if not row:
        return None
    if not verify_password(password, row["password_hash"]):
        return None
    return {"id": row["id"], "email": row["email"], "role": row["role"]}


def issue_session(user_id: int, ip: str, user_agent: str) -> str:
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(hours=SESSION_TTL_HOURS)
    with get_conn() as c:
        c.execute(
            "INSERT INTO sessions(token, user_id, expires_at, ip, user_agent) VALUES (?,?,?,?,?)",
            (token, user_id, expires.isoformat(), ip, user_agent[:500]),
        )
        c.execute(
            "UPDATE users SET last_login_at = datetime('now') WHERE id = ?",
            (user_id,),
        )
    return token


def revoke_session(token: str) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM sessions WHERE token = ?", (token,))


def revoke_all_sessions(user_id: int) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


def session_user(token: Optional[str]) -> Optional[dict]:
    if not token:
        return None
    with get_conn() as c:
        row = c.execute(
            """SELECT u.id, u.email, u.role, s.expires_at
               FROM sessions s JOIN users u ON u.id = s.user_id
               WHERE s.token = ?""",
            (token,),
        ).fetchone()
    if not row:
        return None
    if datetime.fromisoformat(row["expires_at"]) < datetime.now(UTC):
        revoke_session(token)
        return None
    return {"id": row["id"], "email": row["email"], "role": row["role"]}


def current_user() -> Optional[dict]:
    token = request.cookies.get(SESSION_COOKIE)
    return session_user(token)


def login_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u:
            if request.path.startswith("/api/"):
                return jsonify({"success": False, "error": "unauthenticated"}), 401
            return redirect("/login")
        request.user = u
        return f(*args, **kwargs)
    return wrapped


def super_admin_required(f):
    @wraps(f)
    def wrapped(*args, **kwargs):
        u = current_user()
        if not u:
            return jsonify({"success": False, "error": "unauthenticated"}), 401
        if u["role"] != "super_admin":
            return jsonify({"success": False, "error": "super_admin required"}), 403
        request.user = u
        return f(*args, **kwargs)
    return wrapped


def record_login_attempt(ip: str, email: str, success: bool) -> None:
    with get_conn() as c:
        c.execute(
            "INSERT INTO login_attempts(ip, email, success) VALUES (?,?,?)",
            (ip, email, 1 if success else 0),
        )


def login_rate_limited(ip: str) -> bool:
    with get_conn() as c:
        row = c.execute(
            f"""SELECT COUNT(*) AS n FROM login_attempts
                WHERE ip = ? AND success = 0
                AND ts > datetime('now', '-{LOGIN_FAIL_WINDOW_MIN} minutes')""",
            (ip,),
        ).fetchone()
    return int(row["n"]) >= LOGIN_FAIL_MAX


def audit(action: str, target: Optional[str] = None, detail: Optional[str] = None) -> None:
    u = getattr(request, "user", None) or {}
    with get_conn() as c:
        c.execute(
            "INSERT INTO admin_audit(user_id, user_email, action, target, detail, ip) VALUES (?,?,?,?,?,?)",
            (
                u.get("id"),
                u.get("email"),
                action,
                target,
                detail,
                request.remote_addr,
            ),
        )
