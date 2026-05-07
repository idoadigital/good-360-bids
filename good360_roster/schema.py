"""
schema.py — E-Comsetter Good360 Roster System
SQLite Database Schema Generator
All 13 tables as defined in ROSTER_PROJECT_BRIEF.md
Built: 2026-03-20
"""

import sqlite3
import sys
from pathlib import Path

# Add vault to path for key management
sys.path.insert(0, str(Path(__file__).parent))


# ─── Full Schema ───────────────────────────────────────────────────────────────
SCHEMA_SQL = """
-- ============================================================
-- E-Comsetter Good360 Roster System
-- SQLite Database (OS-level encryption via FDE/LUKS)
-- 13 Tables + indexes + triggers
-- ============================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA recursive_triggers = ON;

-- ── Table 1: nonprofits ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS nonprofits (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid                TEXT    NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(16)))),
    org_name            TEXT    NOT NULL,
    contact_name        TEXT    NOT NULL,
    contact_email       TEXT    NOT NULL,
    contact_phone       TEXT    NOT NULL,
    joined_date         TEXT    NOT NULL DEFAULT (date('now')),
    status              TEXT    NOT NULL DEFAULT 'pending_setup'
                        CHECK(status IN (
                            'active','cooldown','suspended','pending_setup'
                        )),
    queue_position      INTEGER DEFAULT 0,
    last_purchase_date  TEXT    NULL,
    cooldown_until      TEXT    NULL,
    total_trucks_found  INTEGER NOT NULL DEFAULT 0,
    subscription_active INTEGER NOT NULL DEFAULT 0,
    subscription_start  TEXT    NULL,
    alert_email         TEXT    NULL,
    phone_number        TEXT    NULL,
    sms_alerts_enabled  INTEGER NOT NULL DEFAULT 0,
    auto_buy_global     INTEGER NOT NULL DEFAULT 0,
    master_card_fallback INTEGER NOT NULL DEFAULT 0,
    agreement_signed    INTEGER NOT NULL DEFAULT 0,
    agreement_signed_date TEXT  NULL,
    max_price_override  REAL    NULL,
    notes               TEXT    NULL,
    created_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_nonprofits_status        ON nonprofits(status);
CREATE INDEX IF NOT EXISTS idx_nonprofits_cooldown_until ON nonprofits(cooldown_until);
CREATE INDEX IF NOT EXISTS idx_nonprofits_queue_position ON nonprofits(queue_position);

-- ── Table 2: nonprofit_credentials ───────────────────────────
CREATE TABLE IF NOT EXISTS nonprofit_credentials (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    nonprofit_id      INTEGER NOT NULL REFERENCES nonprofits(id) ON DELETE CASCADE,
    email             TEXT    NOT NULL,
    password_enc      BLOB    NOT NULL,       -- Fernet-encrypted
    good360_org_id    TEXT    NULL,
    last_login        TEXT    NULL,
    login_verified    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_cred_nonprofit ON nonprofit_credentials(nonprofit_id);

-- ── Table 3: nonprofit_addresses ───────────────────────────────
CREATE TABLE IF NOT EXISTS nonprofit_addresses (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nonprofit_id    INTEGER NOT NULL REFERENCES nonprofits(id) ON DELETE CASCADE,
    address_label   TEXT    NOT NULL DEFAULT 'shipping',
    street_line1    TEXT    NOT NULL,
    street_line2    TEXT    NULL,
    city            TEXT    NOT NULL,
    state           TEXT    NOT NULL,
    zip_code        TEXT    NOT NULL,
    country         TEXT    NOT NULL DEFAULT 'US',
    is_primary      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_addr_nonprofit ON nonprofit_addresses(nonprofit_id);

-- ── Table 4: nonprofit_payment_methods ────────────────────────
-- Up to 3 cards per org: priority 1=primary, 2=backup1, 3=backup2
CREATE TABLE IF NOT EXISTS nonprofit_payment_methods (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    nonprofit_id        INTEGER NOT NULL REFERENCES nonprofits(id) ON DELETE CASCADE,
    priority            INTEGER NOT NULL CHECK(priority BETWEEN 1 AND 3),
    card_holder_name    TEXT    NOT NULL,
    card_number_enc     BLOB    NOT NULL,       -- Fernet-encrypted
    card_last4          TEXT    NOT NULL,
    card_expiry_month   INTEGER NOT NULL,
    card_expiry_year    INTEGER NOT NULL,
    card_cvv_enc        BLOB    NOT NULL,       -- Fernet-encrypted
    billing_zip         TEXT    NOT NULL,
    card_type           TEXT    NULL,
    is_active           INTEGER NOT NULL DEFAULT 1,
    last_used           TEXT    NULL,
    last_declined       TEXT    NULL,
    decline_count       INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pm_nonprofit ON nonprofit_payment_methods(nonprofit_id);

-- ── Table 5: nonprofit_category_preferences ──────────────────
CREATE TABLE IF NOT EXISTS nonprofit_category_preferences (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    nonprofit_id        INTEGER NOT NULL REFERENCES nonprofits(id) ON DELETE CASCADE,
    category_key        TEXT    NOT NULL,
    category_label      TEXT    NOT NULL,
    auto_buy_enabled    INTEGER NOT NULL DEFAULT 0,
    max_price_override  REAL    NULL,
    is_excluded         INTEGER NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_cat_nonprofit_key
    ON nonprofit_category_preferences(nonprofit_id, category_key);

-- ── Table 6: nonprofit_checkout_answers ───────────────────────
CREATE TABLE IF NOT EXISTS nonprofit_checkout_answers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    nonprofit_id    INTEGER NOT NULL REFERENCES nonprofits(id) ON DELETE CASCADE,
    question_key    TEXT    NOT NULL,
    question_label  TEXT    NOT NULL,
    answer_text     TEXT    NOT NULL,
    field_type      TEXT    NOT NULL DEFAULT 'text'
                    CHECK(field_type IN ('text','select','checkbox','textarea')),
    sort_order      INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_checkout_nonprofit ON nonprofit_checkout_answers(nonprofit_id);

-- ── Table 7: truck_events ──────────────────────────────────────
CREATE TABLE IF NOT EXISTS truck_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid            TEXT    NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(16)))),
    detected_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    truck_title     TEXT    NULL,
    truck_url       TEXT    NULL,
    truck_price     REAL    NULL,
    truck_location  TEXT    NULL,
    truck_category  TEXT    NULL,
    raw_data_json   TEXT    NULL,
    status          TEXT    NOT NULL DEFAULT 'detected'
                    CHECK(status IN (
                        'detected','assigned','purchased','missed','unavailable'
                    )),
    assigned_to_org_id  INTEGER NULL REFERENCES nonprofits(id),
    assigned_at     TEXT    NULL,
    notes           TEXT    NULL
);

CREATE INDEX IF NOT EXISTS idx_truck_status     ON truck_events(status);
CREATE INDEX IF NOT EXISTS idx_truck_detected   ON truck_events(detected_at);

-- ── Table 8: purchase_attempts ─────────────────────────────────
CREATE TABLE IF NOT EXISTS purchase_attempts (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid                TEXT    NOT NULL UNIQUE DEFAULT (lower(hex(randomblob(16)))),
    truck_event_id      INTEGER NOT NULL REFERENCES truck_events(id),
    nonprofit_id        INTEGER NOT NULL REFERENCES nonprofits(id),
    payment_method_id   INTEGER NULL REFERENCES nonprofit_payment_methods(id),
    started_at          TEXT    NOT NULL DEFAULT (datetime('now')),
    completed_at        TEXT    NULL,
    status              TEXT    NOT NULL DEFAULT 'in_progress'
                        CHECK(status IN (
                            'in_progress','success','success_mastercard',
                            'failed_payment','failed_login','failed_checkout',
                            'truck_gone','alerted_manual','skipped_excluded','retrying'
                        )),
    mode                TEXT    NOT NULL DEFAULT 'auto_buy'
                        CHECK(mode IN ('auto_buy','alert_only','master_card_fallback')),
    attempt_number      INTEGER NOT NULL DEFAULT 1,
    error_message       TEXT    NULL,
    screenshot_path     TEXT    NULL,
    confirmation_number TEXT    NULL,
    order_total         REAL    NULL,
    cooldown_applied    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pa_truck         ON purchase_attempts(truck_event_id);
CREATE INDEX IF NOT EXISTS idx_pa_nonprofit     ON purchase_attempts(nonprofit_id);
CREATE INDEX IF NOT EXISTS idx_pa_status        ON purchase_attempts(status);

-- ── Table 9: system_payment_methods ───────────────────────────
-- E-Comsetter admin/master cards (catch-all fallback)
CREATE TABLE IF NOT EXISTS system_payment_methods (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    card_label          TEXT    NOT NULL,
    card_holder_name    TEXT    NOT NULL,
    card_number_enc     BLOB    NOT NULL,       -- Fernet-encrypted
    card_last4          TEXT    NOT NULL,
    card_expiry_month   INTEGER NOT NULL,
    card_expiry_year    INTEGER NOT NULL,
    card_cvv_enc        BLOB    NOT NULL,        -- Fernet-encrypted
    billing_zip         TEXT    NOT NULL,
    billing_address     TEXT    NULL,
    card_type           TEXT    NULL,
    is_active           INTEGER NOT NULL DEFAULT 1,
    priority            INTEGER NOT NULL DEFAULT 1,
    total_charged       REAL    NOT NULL DEFAULT 0.0,
    last_used           TEXT    NULL
);

-- ── Table 10: master_card_transactions ─────────────────────────
CREATE TABLE IF NOT EXISTS master_card_transactions (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_attempt_id   INTEGER NOT NULL REFERENCES purchase_attempts(id),
    nonprofit_id          INTEGER NOT NULL REFERENCES nonprofits(id),
    system_payment_id     INTEGER NOT NULL REFERENCES system_payment_methods(id),
    truck_price           REAL    NOT NULL,
    penalty_multiplier    REAL    NOT NULL DEFAULT 3.0,
    penalty_amount        REAL    NOT NULL,
    total_org_owes        REAL    NOT NULL,
    invoice_generated     INTEGER NOT NULL DEFAULT 0,
    invoice_number        TEXT    NULL,
    reimbursed            INTEGER NOT NULL DEFAULT 0,
    reimbursed_date       TEXT    NULL
);

-- ── Table 11: agreements ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS agreements (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    nonprofit_id        INTEGER NOT NULL REFERENCES nonprofits(id) ON DELETE CASCADE,
    agreement_type      TEXT    NOT NULL
                        CHECK(agreement_type IN (
                            'platform_terms','auto_buy_consent',
                            'master_card_consent','subscription_recurring'
                        )),
    version             TEXT    NOT NULL,
    signed_by_name      TEXT    NOT NULL,
    signed_by_email     TEXT    NOT NULL,
    signed_at           TEXT    NOT NULL DEFAULT (datetime('now')),
    ip_address          TEXT    NULL,
    signature_hash      TEXT    NULL,
    document_path       TEXT    NULL,
    is_active           INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_agreement_nonprofit ON agreements(nonprofit_id);

-- ── Table 12: billing_records ───────────────────────────────────
CREATE TABLE IF NOT EXISTS billing_records (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    nonprofit_id          INTEGER NOT NULL REFERENCES nonprofits(id),
    billing_type          TEXT    NOT NULL CHECK(billing_type IN ('subscription','finding_fee')),
    amount                REAL    NOT NULL,
    currency              TEXT    NOT NULL DEFAULT 'USD',
    billing_date          TEXT    NOT NULL DEFAULT (date('now')),
    due_date              TEXT    NULL,
    paid_date             TEXT    NULL,
    status                TEXT    NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','paid','overdue','waived')),
    purchase_attempt_id   INTEGER NULL REFERENCES purchase_attempts(id),
    invoice_number        TEXT    NULL,
    notes                 TEXT    NULL
);

CREATE INDEX IF NOT EXISTS idx_billing_nonprofit ON billing_records(nonprofit_id);
CREATE INDEX IF NOT EXISTS idx_billing_status     ON billing_records(status);

-- ── Table 13: system_events (audit log) ────────────────────────
CREATE TABLE IF NOT EXISTS system_events (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    event_type      TEXT    NOT NULL,
    severity        TEXT    NOT NULL DEFAULT 'info'
                    CHECK(severity IN ('info','warning','error','critical')),
    nonprofit_id    INTEGER NULL REFERENCES nonprofits(id),
    message         TEXT    NOT NULL,
    metadata_json   TEXT    NULL,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sys_event_type   ON system_events(event_type);
CREATE INDEX IF NOT EXISTS idx_sys_severity     ON system_events(severity);
CREATE INDEX IF NOT EXISTS idx_sys_created      ON system_events(created_at);

-- ── Table 14: system_config ────────────────────────────────────
CREATE TABLE IF NOT EXISTS system_config (
    key             TEXT    PRIMARY KEY,
    value           TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- ── Seed default config ────────────────────────────────────────
INSERT OR IGNORE INTO system_config (key, value) VALUES
    ('cooldown_days',                 '7'),
    ('max_orgs_active',               '10'),
    ('subscription_fee_usd',          '200'),
    ('finding_fee_usd',               '500'),
    ('max_payment_fallbacks',          '3'),
    ('scanner_interval_min',           '2'),
    ('master_card_penalty_multiplier', '3.0'),
    ('master_card_enabled',            '1'),
    ('sms_alerts_enabled',              '0'),
    ('twilio_account_sid',             ''),
    ('twilio_auth_token',              ''),
    ('twilio_from_number',             ''),
    ('alert_only_expiry_minutes',      '30'),
    ('default_max_price',            '6400');

-- ── Triggers: auto-update updated_at ───────────────────────────
CREATE TRIGGER IF NOT EXISTS trg_nonprofits_updated
    AFTER UPDATE ON nonprofits
    FOR EACH ROW
    BEGIN
        UPDATE nonprofits SET updated_at = datetime('now') WHERE id = OLD.id;
    END;
"""


def create_db(db_path: str, schema_sql: str = SCHEMA_SQL):
    """
    Create the roster.db SQLite database.
    Uses OS-level full-disk encryption for DB-at-rest security.
    """
    import sqlite3 as sqlite3_native

    db_file = Path(db_path)
    db_file.parent.mkdir(parents=True, exist_ok=True)

    if db_file.exists():
        print(f"[SCHEMA] Database already exists: {db_path}")
        return

    # SQLite connection (OS-level encryption protects DB at rest)
    conn = sqlite3_native.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    conn.executescript(schema_sql)
    conn.commit()
    conn.close()
    print(f"[SCHEMA] Database created: {db_path}")


def verify_db(db_path: str):
    """Verify database tables and row counts."""

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")

    tables = [
        'nonprofits', 'nonprofit_credentials', 'nonprofit_addresses',
        'nonprofit_payment_methods', 'nonprofit_category_preferences',
        'nonprofit_checkout_answers', 'truck_events', 'purchase_attempts',
        'system_payment_methods', 'master_card_transactions',
        'agreements', 'billing_records', 'system_events', 'system_config'
    ]

    print("\n[SCHEMA] Table verification:")
    for table in tables:
        try:
            cur = conn.execute(f"SELECT COUNT(*) FROM {table}")
            count = cur.fetchone()[0]
            print(f"  ✓ {table}: {count} rows")
        except Exception as e:
            print(f"  ✗ {table}: ERROR - {e}")

    conn.close()


if __name__ == "__main__":
    db_path = Path(__file__).parent / "db" / "roster.db"
    create_db(str(db_path))
    verify_db(str(db_path))
