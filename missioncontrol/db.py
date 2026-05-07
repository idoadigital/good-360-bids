"""SQLite store for the admin dashboard.

Holds:
  - users          : accounts (first registered → super-admin)
  - sessions       : active server-side session tokens (so logout is real)
  - settings       : encrypted env values (card data, tokens, passwords)
  - admin_audit    : every privileged action (user create/delete, settings write,
                     service restart). Append-only from the dashboard's POV.

All other run-time data (scans, truck logs, purchase audit) stays in the
existing JSON / JSONL files written by the python scripts. The dashboard
*reads* those — it does not duplicate them.
"""
from __future__ import annotations

import os
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path

WORKDIR = os.environ.get("WORKDIR", "/app/workdir")
DB_PATH = Path(os.environ.get("DASHBOARD_DB", f"{WORKDIR}/dashboard.db"))

_lock = threading.Lock()


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL CHECK(role IN ('super_admin','admin')),
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    created_by    INTEGER REFERENCES users(id),
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    token       TEXT PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    expires_at  TEXT NOT NULL,
    ip          TEXT,
    user_agent  TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);

CREATE TABLE IF NOT EXISTS settings (
    key         TEXT PRIMARY KEY,
    value_enc   BLOB NOT NULL,
    is_secret   INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_by  INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS admin_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    user_id    INTEGER REFERENCES users(id),
    user_email TEXT,
    action     TEXT NOT NULL,
    target     TEXT,
    detail     TEXT,
    ip         TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON admin_audit(ts);

CREATE TABLE IF NOT EXISTS login_attempts (
    ip         TEXT NOT NULL,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    success    INTEGER NOT NULL,
    email      TEXT
);
CREATE INDEX IF NOT EXISTS idx_login_attempts_ip_ts ON login_attempts(ip, ts);

-- ============================================================
-- QuickBeed customer-sync mirror.
-- We never store partner_credentials or card_number/cvv locally.
-- Those are fetched on-demand from /customers/{id}?reason=... at use time.
-- ============================================================

CREATE TABLE IF NOT EXISTS customers (
    id                  TEXT PRIMARY KEY,                -- QuickBeed UUID
    status              TEXT NOT NULL,                   -- active|paused|inactive|onboarding|suspended
    status_reason       TEXT,
    created_at          TEXT,                            -- from QuickBeed
    updated_at          TEXT,                            -- from QuickBeed (ISO8601)
    last_synced_at      TEXT NOT NULL DEFAULT (datetime('now')),
    last_etag           TEXT,
    -- profile
    full_name           TEXT,
    organization_name   TEXT,
    email               TEXT,
    phone               TEXT,
    -- operations
    warehouse_address   TEXT,
    has_loading_dock    INTEGER,
    has_pallet_capability INTEGER,
    distribution_method TEXT,
    people_served       INTEGER,
    preferred_location  TEXT,
    open_to_alternatives INTEGER,
    truck_selection     TEXT,
    priority_level      TEXT,                            -- high|normal|low|null
    max_budget          REAL,
    -- bookkeeping
    in_rotation         INTEGER NOT NULL DEFAULT 1,      -- local override; ANDed with status=='active'
    cooldown_until      TEXT,                            -- local: post-purchase cooldown
    last_used_at        TEXT,                            -- local: last round-robin pick
    last_purchase_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_customers_status ON customers(status);
CREATE INDEX IF NOT EXISTS idx_customers_updated ON customers(updated_at);

CREATE TABLE IF NOT EXISTS webhook_events (
    event_id    TEXT PRIMARY KEY,                        -- X-Event-Id (UUID v4)
    event       TEXT NOT NULL,                           -- customer.created|updated|status_changed
    customer_id TEXT,
    received_at TEXT NOT NULL DEFAULT (datetime('now')),
    processed_at TEXT,
    body_sha256 TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_events_customer ON webhook_events(customer_id);

CREATE TABLE IF NOT EXISTS sync_state (
    key          TEXT PRIMARY KEY,
    value        TEXT,
    updated_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
"""


def _ensure_dir() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)


@contextmanager
def get_conn():
    _ensure_dir()
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    with _lock, get_conn() as c:
        c.executescript(SCHEMA)


def has_any_user() -> bool:
    with get_conn() as c:
        row = c.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        return row is not None


def count_users() -> int:
    with get_conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])
