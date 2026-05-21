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
    last_purchase_at    TEXT,
    manual_queue_position INTEGER                        -- operator's drag-and-drop order; NULL = unranked (LRU fallback)
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

-- ============================================================
-- Notifications: every outbound Telegram message gets recorded
-- here so the admin UI can mirror what operators see in chat.
-- Body is plaintext (it's the same text already in Telegram —
-- not a secret).
-- ============================================================

CREATE TABLE IF NOT EXISTS notifications (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         TEXT NOT NULL DEFAULT (datetime('now')),
    source     TEXT NOT NULL,                     -- monitor|autobuy|watchdog|report|deadman
    level      TEXT NOT NULL,                     -- info|warn|error|success
    channel    TEXT,                              -- operator|org:<key>|null
    title      TEXT,                              -- first line of message
    message    TEXT NOT NULL,                     -- full body sent to telegram
    delivered  INTEGER NOT NULL DEFAULT 0,        -- 1 if API returned ok
    error      TEXT
);
CREATE INDEX IF NOT EXISTS idx_notifications_ts     ON notifications(ts);
CREATE INDEX IF NOT EXISTS idx_notifications_level  ON notifications(level);
CREATE INDEX IF NOT EXISTS idx_notifications_source ON notifications(source);

-- ============================================================
-- Test runs: admin-triggered checkout smoke tests.
-- We never persist card PAN or CVV — only brand + last4 for the
-- audit trail. Full PAN lives in memory only for the duration of
-- a Playwright session.
-- ============================================================

CREATE TABLE IF NOT EXISTS test_runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    ts               TEXT NOT NULL DEFAULT (datetime('now')),
    started_at       TEXT,
    finished_at      TEXT,
    status           TEXT NOT NULL,        -- stubbed|queued|running|completed|failed
    customer_name    TEXT,
    customer_email   TEXT,
    truck_url        TEXT,
    card_brand       TEXT,
    card_last4       TEXT,                  -- 4 chars, never the full PAN
    result_summary   TEXT,                  -- one-line outcome from the runner
    error            TEXT,                  -- multi-line error / page text
    screenshot_path  TEXT,                  -- workdir-relative path if captured
    created_by_user_id INTEGER REFERENCES users(id)
);
CREATE INDEX IF NOT EXISTS idx_test_runs_ts ON test_runs(ts);

-- ============================================================
-- Good360 login attempts: every time the monitor (or any worker)
-- tries to authenticate against Good360. NOT to be confused with
-- the dashboard's own login_attempts table above (which tracks
-- admin-login rate limiting). Used by the Scans page's "Login
-- health" panel.
-- ============================================================

CREATE TABLE IF NOT EXISTS good360_login_attempts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL DEFAULT (datetime('now')),
    source      TEXT NOT NULL,        -- monitor|autobuy|test_run|manual
    email       TEXT,                 -- email used to attempt login
    success     INTEGER NOT NULL,     -- 1 / 0
    duration_ms INTEGER,              -- end-to-end time including page loads
    error       TEXT                  -- exception/page text on failure
);
CREATE INDEX IF NOT EXISTS idx_g360_login_ts ON good360_login_attempts(ts);

-- ============================================================
-- Scans: one row per monitor scan. Replaces the rewrite-on-every-
-- scan good360_run_log.json file as the source of truth for
-- analytics. The trucks observed in each scan are stored as a
-- denormalized JSON blob — fine for our query volumes, and avoids
-- a child table and join for every analytics request.
-- ============================================================

CREATE TABLE IF NOT EXISTS scans (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,            -- when the scan started (ISO-8601)
    alert_sent   INTEGER NOT NULL DEFAULT 0,
    action       TEXT,                     -- e.g. 'auto_buy_attempt', empty if no-op
    truck_count  INTEGER NOT NULL DEFAULT 0,
    available_count INTEGER NOT NULL DEFAULT 0,
    trucks_json  TEXT NOT NULL DEFAULT '[]'  -- list of {name, available, tracked}
);
CREATE INDEX IF NOT EXISTS idx_scans_ts ON scans(ts);

-- Per-product state: the operator's per-truck-name tracking/autobuy flags.
-- The monitor upserts a row on every observation; the dashboard mutates the
-- flags via the Scans → Observed products UI. `last_url` is what filters
-- this table by sandbox-vs-live (rows whose url contains sandbox-360 are
-- only shown in sandbox mode, etc.).
CREATE TABLE IF NOT EXISTS tracked_products (
    name             TEXT PRIMARY KEY,
    tracked          INTEGER NOT NULL DEFAULT 1,    -- 1 = scan + alert; 0 = grayed out, ignored entirely
    autobuy_enabled  INTEGER NOT NULL DEFAULT 0,    -- 1 = attempt purchase when available
    last_url         TEXT,
    last_price       REAL,                          -- last price scraped (NULL when account can't see prices)
    manual_price     REAL,                          -- operator override; takes precedence over last_price
    description      TEXT,
    first_seen       TEXT,
    last_seen        TEXT,
    updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_tracked_products_last_seen ON tracked_products(last_seen);

-- Every autobuy attempt the legacy monitor (good360_monitor.py + autobuy.py)
-- makes. Denormalized on purpose so the monitor doesn't have to populate
-- truck_events / nonprofits / payment_methods to log a row. The Purchases
-- page UNIONs this with the v2 roster.db.purchase_attempts table to give
-- one consolidated history regardless of which engine ran.
CREATE TABLE IF NOT EXISTS legacy_purchase_attempts (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                   TEXT NOT NULL DEFAULT (datetime('now')),
    status               TEXT NOT NULL,         -- SUCCESS|FAILED|MISSED|MANUAL|COOLDOWN|LOCKED|ERROR|...
    engine               TEXT,                  -- daemon|script|devtools_agent
    org_name             TEXT,                  -- legacy org config name (e.g. Reviving Homes Foundation)
    customer_id          TEXT,                  -- QuickBeed UUID if a customer was queued, else NULL
    customer_name        TEXT,
    truck_name           TEXT,
    truck_url            TEXT,
    truck_price          REAL,
    order_total          REAL,
    confirmation_number  TEXT,
    error_message        TEXT,
    capture_path         TEXT,                  -- workdir-relative path into checkout_captures/
    screenshot_path      TEXT,
    elapsed_seconds      REAL
);
CREATE INDEX IF NOT EXISTS idx_legacy_pa_ts ON legacy_purchase_attempts(ts);

-- AI-generated diagnosis for failed purchases. Keyed by (source, attempt_id)
-- so a single attempt only ever incurs one Claude call. `model` is recorded
-- for evolvability — when we switch models, regenerate where it'd help.
CREATE TABLE IF NOT EXISTS purchase_diagnoses (
    source            TEXT NOT NULL,        -- 'roster' | 'legacy'
    attempt_id        INTEGER NOT NULL,
    diagnosis         TEXT NOT NULL,
    suggested_action  TEXT,
    model             TEXT,
    similar_count     INTEGER NOT NULL DEFAULT 0,
    input_tokens      INTEGER,
    output_tokens     INTEGER,
    cache_read_tokens INTEGER,
    created_at        TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (source, attempt_id)
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
        _apply_migrations(c)


def _apply_migrations(c) -> None:
    """Idempotent ALTER TABLE migrations for columns added after the
    initial CREATE. SQLite's CREATE TABLE IF NOT EXISTS only acts on
    fresh databases — for existing dbs we need to add columns explicitly.
    Each migration is wrapped in try/except so re-runs are no-ops."""
    migrations = [
        # 2026-05-12: operator-set manual price (account can't see prices)
        "ALTER TABLE tracked_products ADD COLUMN manual_price REAL",
        # 2026-05-14: drag-and-drop queue ordering; NULL = unranked, LRU fallback
        "ALTER TABLE customers ADD COLUMN manual_queue_position INTEGER",
    ]
    for sql in migrations:
        try:
            c.execute(sql)
        except sqlite3.OperationalError:
            # Column already exists — that's the expected steady state.
            pass


def has_any_user() -> bool:
    with get_conn() as c:
        row = c.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        return row is not None


def count_users() -> int:
    with get_conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM users").fetchone()
        return int(row["n"])
