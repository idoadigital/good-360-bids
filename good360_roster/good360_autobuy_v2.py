"""
good360_autobuy_v2.py — E-Comsetter Good360 Roster System
Multi-Account Credentialized Checkout Engine
Built: 2026-03-20

Replaces good360_autobuy.py with org-parameterized checkout.
Coordinates with: vault.py (credentials), queue_manager.py, notifier.py,
billing_manager.py, roster_orchestrator.py
"""

import json
import logging
import os
import sqlite3
import sys
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("autobuy_v2")

# ─── Paths ──────────────────────────────────────────────────────────────────
DB_PATH = Path(__file__).parent / "db" / "roster.db"
DOWNLOAD_DIR = Path("/a0/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ─── Vault Import ────────────────────────────────────────────────────────────
@contextmanager
def _get_vault():
    sys.path.insert(0, str(Path(__file__).parent))
    from vault import decrypt_field, load_env
    load_env()
    yield decrypt_field


def decrypt_cred(blob: bytes) -> str:
    """Decrypt a Fernet-encrypted credential field."""
    with _get_vault() as decrypt:
        return decrypt(blob)


# ─── DB ──────────────────────────────────────────────────────────────────────
@contextmanager
def get_db_connection():
    conn = sqlite3.connect(str(DB_PATH), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


def get_config(key: str, default: str = None) -> str:
    with get_db_connection() as conn:
        row = conn.execute("SELECT value FROM system_config WHERE key = ?",
                          (key,)).fetchone()
        return row[0] if row else default


# Sandbox-mode router — when SANDBOX_MODE=true, every OrgContext gets its
# Good360 creds and payment_methods rewritten to point at the sandbox account
# / test card. Imported here so the override applies whether the OrgContext
# came from QuickBeed (live API) or roster.db (legacy).
import sys as _sys
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in _sys.path:
    _sys.path.insert(0, _REPO_ROOT)
import sandbox  # noqa: E402


def _apply_sandbox_overrides(ctx: "OrgContext") -> "OrgContext":
    """Swap live creds + cards for sandbox values when SANDBOX_MODE is on.

    Idempotent. In live mode, returns ctx unchanged.
    """
    if not sandbox.is_sandbox():
        return ctx
    sb_email, sb_password = sandbox.org_credentials(ctx.good360_email, ctx.good360_password)
    sb_card = sandbox.card_for_org(None) or {}
    try:
        exp_month = int(sb_card.get("expiry", "1230")[:2])
        exp_year = 2000 + int(sb_card.get("expiry", "1230")[2:])
    except (ValueError, TypeError):
        exp_month, exp_year = 12, 2030
    ctx.good360_email = sb_email
    ctx.good360_password = sb_password
    ctx.payment_methods = [{
        "id": None,
        "priority": 1,
        "card_holder_name": sb_card.get("name", "Sandbox Tester"),
        "card_number": sb_card.get("number", ""),
        "card_last4": (sb_card.get("number") or "")[-4:],
        "card_expiry_month": exp_month,
        "card_expiry_year": exp_year,
        "card_cvv": sb_card.get("cvv", ""),
        "billing_zip": "30046",
        "card_type": sb_card.get("type", "visa"),
    }]
    return ctx


# ─── Data Classes ─────────────────────────────────────────────────────────────
@dataclass
class OrgContext:
    """Complete org context for a checkout run."""
    org_id: int
    org_uuid: str
    org_name: str
    contact_name: str
    contact_email: str
    alert_email: str
    phone_number: str
    sms_alerts_enabled: bool
    auto_buy_global: bool
    master_card_fallback: bool
    max_price_override: float | None
    good360_email: str
    good360_password: str
    good360_org_id: str | None
    addresses: list[dict] = field(default_factory=list)
    payment_methods: list[dict] = field(default_factory=list)
    category_prefs: dict[str, dict] = field(default_factory=dict)
    checkout_answers: dict[str, str] = field(default_factory=dict)
    billing_address: dict = field(default_factory=dict)


@dataclass
class TruckContext:
    """Truck being purchased."""
    truck_event_id: int
    truck_uuid: str
    truck_title: str
    truck_url: str
    truck_price: float
    truck_location: str
    truck_category: str
    raw_data_json: str


@dataclass
class CheckoutResult:
    """Result of a checkout attempt."""
    success: bool
    status: str  # purchase_attempts.status
    mode: str    # auto_buy / alert_only / master_card_fallback
    confirmation_number: str | None = None
    order_total: float | None = None
    error_message: str | None = None
    payment_method_id: int | None = None
    attempt_number: int = 1
    screenshot_path: str | None = None


# ─── Load Org Context ────────────────────────────────────────────────────────
# ─── QuickBeed-backed org context (calls missioncontrol's internal API) ─────
#
# For QuickBeed-sourced nonprofits we don't store credentials or card data
# in roster.db. Instead, at purchase time we ask missioncontrol for the live
# org_config — it has the encrypted master key and is the only process
# allowed to decrypt the QuickBeed token and call /customers/{id}.
#
# The internal endpoint requires an X-API-Key matching MISSIONCONTROL_API_KEY
# (already in shared .env). missioncontrol serves HTTPS on a self-signed cert
# inside the Docker network — we set verify=False since this is intra-cluster.

def _load_org_context_quickbeed(org_row, quickbeed_customer_id: str,
                                 *, require_payment_methods: bool = True) -> OrgContext:
    import requests  # local import — keeps top-level imports unchanged for legacy path

    base = os.environ.get("MISSIONCONTROL_INTERNAL_URL", "https://missioncontrol:5001")
    api_key = os.environ.get("MISSIONCONTROL_API_KEY", "")
    if not api_key:
        raise RuntimeError("MISSIONCONTROL_API_KEY not set; cannot fetch QuickBeed org config")

    url = f"{base}/api/internal/org-config/{quickbeed_customer_id}"
    resp = requests.get(
        url,
        params={"reason": "credential_use"},
        headers={"X-API-Key": api_key},
        timeout=15,
        verify=False,  # self-signed cert on the internal Docker network
    )
    if resp.status_code != 200:
        raise ValueError(f"missioncontrol /org-config returned {resp.status_code}: {resp.text[:300]}")
    payload = resp.json()
    if not payload.get("success"):
        raise ValueError(f"missioncontrol /org-config error: {payload.get('error')}")
    cfg = payload["org_config"]

    if not cfg.get("good360_email") or not cfg.get("good360_password"):
        raise ValueError(
            f"QuickBeed customer {quickbeed_customer_id} has no usable partner_credentials "
            f"(status={payload.get('status')}); refusing to attempt purchase"
        )

    primary = cfg.get("card") or {}
    fallbacks = cfg.get("fallback_cards") or []
    payment_methods = []
    for prio, c in enumerate([primary] + fallbacks, start=1):
        if not c or not c.get("number"):
            continue
        try:
            exp_month = int(c["expiry"][:2])
            exp_year_full = 2000 + int(c["expiry"][2:])
        except (KeyError, ValueError, TypeError):
            logger.warning(f"QB card #{prio} has unparseable expiry — skipping")
            continue
        billing = c.get("billing") or {}
        payment_methods.append({
            "id": None,                    # no local DB row
            "priority": prio,
            "card_holder_name": c.get("name") or cfg.get("name"),
            "card_number": c.get("number"),
            "card_last4": (c.get("number") or "")[-4:],
            "card_expiry_month": exp_month,
            "card_expiry_year": exp_year_full,
            "card_cvv": c.get("cvv"),
            "billing_zip": billing.get("zip", ""),
            "card_type": c.get("type"),
        })

    if not payment_methods and require_payment_methods:
        raise ValueError(
            f"QuickBeed customer {quickbeed_customer_id} has no usable payment_methods; "
            "refusing to attempt purchase"
        )

    addresses = []
    if cfg.get("warehouse_address"):
        addresses.append({
            "id": None, "address_label": "warehouse",
            "address_line": cfg["warehouse_address"],
            "is_primary": 1,
        })

    return _apply_sandbox_overrides(OrgContext(
        org_id=org_row["id"],
        org_uuid=org_row["uuid"],
        org_name=cfg.get("name") or org_row["org_name"],
        contact_name=org_row["contact_name"],
        contact_email=org_row["contact_email"],
        alert_email=org_row["alert_email"] or org_row["contact_email"],
        phone_number=org_row["phone_number"] or "",
        sms_alerts_enabled=False,
        auto_buy_global=bool(org_row["auto_buy_global"]),
        master_card_fallback=False,
        max_price_override=cfg.get("max_price") or org_row["max_price_override"],
        good360_email=cfg["good360_email"],
        good360_password=cfg["good360_password"],
        good360_org_id=None,
        addresses=addresses,
        payment_methods=payment_methods,
        category_prefs={},
        checkout_answers=cfg.get("checkout_answers") or {},
        billing_address=cfg.get("billing_address") or {},
    ))


def load_org_context(org_id: int, *, require_payment_methods: bool = True) -> OrgContext:
    """
    Load full org context.

    For legacy nonprofits (no `quickbeed_customer_id`), reads + decrypts from
    the local roster.db nonprofit_credentials / nonprofit_payment_methods
    tables (existing behavior, unchanged).

    For QuickBeed-sourced nonprofits, calls missioncontrol's internal API to
    pull the full record live — credentials and card data are never persisted
    locally. The plaintext lives in memory only for the duration of the
    purchase attempt and is discarded with the OrgContext.
    """
    with get_db_connection() as conn:
        org = conn.execute("SELECT * FROM nonprofits WHERE id = ?", (org_id,)).fetchone()
        if not org:
            raise ValueError(f"Org {org_id} not found")

        org_keys = org.keys() if hasattr(org, "keys") else []
        qb_id = org["quickbeed_customer_id"] if "quickbeed_customer_id" in org_keys else None
        if qb_id:
            return _load_org_context_quickbeed(
                org, qb_id, require_payment_methods=require_payment_methods,
            )

        cred = conn.execute(
            "SELECT * FROM nonprofit_credentials WHERE nonprofit_id = ?", (org_id,)
        ).fetchone()

        addrs = conn.execute(
            "SELECT * FROM nonprofit_addresses WHERE nonprofit_id = ? ORDER BY is_primary DESC",
            (org_id,)
        ).fetchall()

        cards = conn.execute(
            """SELECT * FROM nonprofit_payment_methods
               WHERE nonprofit_id = ? AND is_active=1
               ORDER BY priority ASC""",
            (org_id,)
        ).fetchall()

        cats = conn.execute(
            "SELECT * FROM nonprofit_category_preferences WHERE nonprofit_id = ?",
            (org_id,)
        ).fetchall()

        answers = conn.execute(
            "SELECT question_key, answer_text FROM nonprofit_checkout_answers WHERE nonprofit_id = ?",
            (org_id,)
        ).fetchall()

    if not cred:
        raise ValueError(f"Org {org_id} has no credentials — cannot checkout")

    # Decrypt sensitive fields
    with _get_vault() as decrypt:
        password = decrypt(cred["password_enc"])
        cards_decrypted = []
        for card in cards:
            cards_decrypted.append({
                "id": card["id"],
                "priority": card["priority"],
                "card_holder_name": card["card_holder_name"],
                "card_number": decrypt(card["card_number_enc"]),
                "card_last4": card["card_last4"],
                "card_expiry_month": card["card_expiry_month"],
                "card_expiry_year": card["card_expiry_year"],
                "card_cvv": decrypt(card["card_cvv_enc"]),
                "billing_zip": card["billing_zip"],
                "card_type": card["card_type"],
            })

    return _apply_sandbox_overrides(OrgContext(
        org_id=org["id"],
        org_uuid=org["uuid"],
        org_name=org["org_name"],
        contact_name=org["contact_name"],
        contact_email=org["contact_email"],
        alert_email=org["alert_email"] or org["contact_email"],
        phone_number=org["phone_number"] or "",
        sms_alerts_enabled=bool(org["sms_alerts_enabled"]),
        auto_buy_global=bool(org["auto_buy_global"]),
        master_card_fallback=bool(org["master_card_fallback"]),
        max_price_override=org["max_price_override"],
        good360_email=cred["email"],
        good360_password=password,
        good360_org_id=cred["good360_org_id"],
        addresses=[dict(a) for a in addrs],
        payment_methods=cards_decrypted,
        category_prefs={c["category_key"]: dict(c) for c in cats},
        checkout_answers={a["question_key"]: a["answer_text"] for a in answers},
    ))


# ─── Load Truck Context ──────────────────────────────────────────────────────
def load_truck_context(truck_event_id: int) -> TruckContext:
    with get_db_connection() as conn:
        truck = conn.execute("SELECT * FROM truck_events WHERE id = ?",
                            (truck_event_id,)).fetchone()
    if not truck:
        raise ValueError(f"Truck event {truck_event_id} not found")

    return TruckContext(
        truck_event_id=truck["id"],
        truck_uuid=truck["uuid"],
        truck_title=truck["truck_title"] or "Unknown Truck",
        truck_url=truck["truck_url"] or "",
        truck_price=truck["truck_price"] or 0.0,
        truck_location=truck["truck_location"] or "",
        truck_category=truck["truck_category"] or "other",
        raw_data_json=truck["raw_data_json"] or "{}",
    )


# ─── Record Purchase Attempt ──────────────────────────────────────────────────
def create_purchase_attempt(org_id: int, truck_event_id: int,
                           payment_method_id: int = None,
                           mode: str = "auto_buy") -> int:
    """Create a purchase_attempt record, return its ID."""
    with get_db_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO purchase_attempts
               (truck_event_id, nonprofit_id, payment_method_id, mode, status)
               VALUES (?, ?, ?, ?, 'in_progress')""",
            (truck_event_id, org_id, payment_method_id, mode)
        )
        conn.commit()
        return cursor.lastrowid


def update_purchase_attempt(attempt_id: int, status: str,
                            confirmation_number: str = None,
                            order_total: float = None,
                            error_message: str = None,
                            screenshot_path: str = None,
                            cooldown_applied: bool = False):
    """Finalize a purchase_attempt record."""
    with get_db_connection() as conn:
        conn.execute(
            """UPDATE purchase_attempts
               SET status=?, completed_at=datetime('now'),
                   confirmation_number=?, order_total=?,
                   error_message=?, screenshot_path=?,
                   cooldown_applied=?
               WHERE id=?""",
            (status, confirmation_number, order_total,
             error_message, screenshot_path,
             int(cooldown_applied), attempt_id)
        )
        conn.commit()


def log_system_event(event_type: str, severity: str, org_id: int = None,
                     message: str = "", metadata: dict = None):
    import json
    with get_db_connection() as conn:
        conn.execute(
            """INSERT INTO system_events
               (event_type, severity, nonprofit_id, message, metadata_json)
               VALUES (?, ?, ?, ?, ?)""",
            (event_type, severity, org_id, message, json.dumps(metadata) if metadata else None)
        )
        conn.commit()


# ─── Payment Method Selection ────────────────────────────────────────────────
def get_active_payment_methods(org: OrgContext) -> list[dict]:
    """Return active, non-declined payment methods."""
    active = []
    for card in org.payment_methods:
        if card["priority"] > 3:
            continue
        # Load decline info from DB
        with get_db_connection() as conn:
            row = conn.execute(
                """SELECT decline_count, last_declined FROM nonprofit_payment_methods
                   WHERE id=?""", (card["id"],)
            ).fetchone()
        # Soft block: if >5 declines in a row, skip (might be card issue)
        if row and row["decline_count"] > 5:
            logger.warning(f"Card {card['id']} has {row['decline_count']} declines — skipping")
            continue
        active.append(card)
    return active


def record_card_decline(payment_method_id: int):
    with get_db_connection() as conn:
        conn.execute(
            """UPDATE nonprofit_payment_methods
               SET decline_count = decline_count + 1,
                   last_declined = datetime('now')
               WHERE id = ?""",
            (payment_method_id,)
        )
        conn.commit()


# ─── Browser Checkout ─────────────────────────────────────────────────────────
class _DevToolsMCPUnavailable(RuntimeError):
    """Raised when good360_devtools_agent or the openai-agents SDK can't be
    loaded. Caller falls back to the legacy browser_agent path."""


def _run_checkout_via_devtools_mcp(org: OrgContext, truck: TruckContext,
                                   payment_card: dict,
                                   *, test_mode: bool = False) -> CheckoutResult:
    """Production checkout dispatched through the Chrome DevTools MCP agent.

    Builds an `org_override` dict from the QuickBeed-fetched OrgContext and
    the operator-selected payment card, then invokes
    good360_devtools_agent.run_agent. Maps CheckoutAgentResult onto our
    CheckoutResult shape so the surrounding code path (DB rows, billing,
    notifications, cooldown) is unchanged.

    Live-purchase mode (dry_run=False) requires
    DEVTOOLS_AGENT_ALLOW_LIVE_PURCHASE + DEVTOOLS_AGENT_ALLOW_SECRETS_TO_MODEL
    to be true in Settings. The agent's _validate_purchase_context enforces
    this — we surface the RuntimeError verbatim as a failed_checkout result
    so the operator sees exactly which flag is missing.
    """
    import asyncio as _asyncio
    import sys as _sys

    # Make the repo root importable so we can pick up good360_devtools_agent.
    for _p in ("/app", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))):
        if _p not in _sys.path:
            _sys.path.insert(0, _p)
    try:
        import good360_devtools_agent as _agent
    except Exception as exc:
        raise _DevToolsMCPUnavailable(f"import good360_devtools_agent failed: {exc}") from exc

    # Card expiry: production stores month/year separately. The agent's prompt
    # builder hands the card to the LLM verbatim, so we format MMYY here so
    # the model can fill `MM / YY` checkout inputs without translation.
    expiry_mmyy = f"{int(payment_card['card_expiry_month']):02d}{str(payment_card['card_expiry_year'])[-2:]}"

    # Pick the primary warehouse + billing addresses from the QB-supplied list.
    primary_addr = next((a for a in org.addresses if a.get("is_primary")), None) \
                   or (org.addresses[0] if org.addresses else {})
    # Address dict keys differ across sources:
    #   • QuickBeed loader writes `address_line` (single line, free form)
    #   • Legacy roster.db.nonprofit_addresses uses `street_line1`/`city`/`state`/`zip_code`
    #   • Older callers used `address1` / `address`
    # The agent prompt accepts any single-string warehouse_address, so collapse
    # whichever shape we got into one string. Without this, the QuickBeed path
    # passed `""` and the agent stalled on the Shipping address step.
    warehouse_address = (
        primary_addr.get("address1")
        or primary_addr.get("address")
        or primary_addr.get("address_line")
        or " ".join(p for p in (
            primary_addr.get("street_line1"),
            primary_addr.get("street_line2"),
            primary_addr.get("city"),
            primary_addr.get("state"),
            primary_addr.get("zip_code"),
        ) if p)
        or ""
    )
    billing_address   = (payment_card.get("billing_zip") or "").strip()

    org_override = {
        "name":              org.org_name,
        "good360_email":     org.good360_email,
        "good360_password":  org.good360_password,
        "card": {
            "name":   payment_card.get("card_holder_name") or org.contact_name or org.org_name,
            "number": payment_card["card_number"],
            "expiry": expiry_mmyy,
            "cvv":    payment_card["card_cvv"],
            "type":   payment_card.get("card_type", "visa"),
        },
        "checkout_answers":  dict(org.checkout_answers or {}),
        "warehouse_address": warehouse_address,
        "billing_address":   billing_address,
        "buyer_name":        org.contact_name or org.org_name,
        "buyer_email":       org.contact_email,
        "max_auto_pay":      float(org.max_price_override or os.environ.get("MAX_AUTO_PAY", "6400")),
    }

    # Dry-run gate: respects QUICKBEED_DRY_RUN for QB-sourced orgs in production
    # so staging keeps not spending money. In test_mode we already swapped in a
    # fake card and suppressed side effects upstream — clicking Place Order is
    # safe and required so we can observe the real card-decline response.
    qb_dry_run = (os.environ.get("QUICKBEED_DRY_RUN", "").lower() in ("1", "true", "yes"))
    is_qb = getattr(org, "good360_org_id", "sentinel") is None
    dry_run = (qb_dry_run and is_qb) and not test_mode

    logger.info(
        "[MCP] dispatching to chrome-devtools-mcp agent · org=%s truck=%s dry_run=%s card=****%s",
        org.org_id, truck.truck_event_id, dry_run, str(payment_card["card_number"])[-4:],
    )

    try:
        agent_result = _asyncio.run(_agent.run_agent(
            org_key=str(org.org_id),
            truck_name=truck.truck_title,
            truck_url=truck.truck_url,
            admin_fee=float(truck.truck_price or 0.0),
            dry_run=dry_run,
            org_override=org_override,
        ))
    except RuntimeError as exc:
        # _validate_purchase_context surfaces missing safety flags / creds
        # as RuntimeError. Translate to a failed_checkout so the surrounding
        # retry/notification logic handles it normally.
        return CheckoutResult(
            success=False,
            status="failed_checkout",
            mode="auto_buy",
            error_message=f"[MCP] {exc}",
        )
    except Exception as exc:
        return CheckoutResult(
            success=False,
            status="failed_checkout",
            mode="auto_buy",
            error_message=f"[MCP] agent crashed: {type(exc).__name__}: {exc}",
        )

    # Map CheckoutAgentResult.status → autobuy_v2 statuses.
    a_status = (getattr(agent_result, "status", "FAILED") or "FAILED").upper()
    a_msg    = getattr(agent_result, "message", "") or ""
    confirmation = getattr(agent_result, "confirmation_number", None)
    order_total  = getattr(agent_result, "order_total", None)

    if a_status == "SUCCESS":
        # PROOF REQUIRED: on 2026-06-12 the agent reported SUCCESS with no
        # confirmation number for an order that never existed on Good360 —
        # the system then marked the truck purchased, emailed the customer
        # "Purchase Confirmed", and applied a 7-day cooldown, all on a
        # fabricated claim. A success without a confirmation number is
        # treated as a FAILURE so no downstream side effect can fire on an
        # unproven purchase.
        if not (confirmation and str(confirmation).strip()):
            logger.error(
                "[MCP] agent claimed SUCCESS without a confirmation number — "
                "treating as failed_checkout (org=%s truck=%s). Agent message: %s",
                org.org_id, truck.truck_event_id, a_msg)
            return CheckoutResult(
                success=False, status="failed_checkout", mode="auto_buy",
                order_total=order_total,
                error_message=("[MCP] UNVERIFIED SUCCESS REJECTED: agent reported "
                               "success but provided no order confirmation number. "
                               f"Agent message: {a_msg}"),
            )
        return CheckoutResult(
            success=True, status="success", mode="auto_buy",
            confirmation_number=confirmation,
            order_total=order_total or float(truck.truck_price or 0.0),
            error_message=None,
        )
    if a_status == "DRY_RUN":
        # Mirrors the existing dry_run synthetic-success shape.
        return CheckoutResult(
            success=True, status="dry_run_ok", mode="auto_buy",
            confirmation_number=f"DRYRUN-MCP-{org.org_id}-{truck.truck_event_id}",
            order_total=order_total or float(truck.truck_price or 0.0),
            error_message=None,
        )
    if a_status == "MISSED":
        # purchase_attempts.status CHECK constraint accepts 'truck_gone' for
        # the "truck became unavailable" case, not 'missed' (that's the
        # truck_events table's status). Mismatch crashed the row update.
        return CheckoutResult(success=False, status="truck_gone", mode="auto_buy",
                             error_message=f"[MCP] {a_msg}")
    if a_status == "MANUAL":
        return CheckoutResult(success=False, status="alerted_manual", mode="auto_buy",
                             error_message=f"[MCP] {a_msg}")
    if a_status == "BLOCKED":
        return CheckoutResult(success=False, status="failed_checkout", mode="auto_buy",
                             error_message=f"[MCP] BLOCKED: {a_msg}")
    return CheckoutResult(success=False, status="failed_checkout", mode="auto_buy",
                         error_message=f"[MCP] {a_status}: {a_msg}")


def _run_checkout_via_daemon(org: OrgContext, truck: TruckContext,
                             payment_card: dict,
                             *, test_mode: bool = False) -> CheckoutResult:
    """Deterministic checkout via the daemon's Playwright executor
    (good360_daemon.py::_checkout_inner) — the path verified end-to-end with
    screenshots and an evidence-based success heuristic (URL change, order-#
    regex, explicit thank-you text). Replaces the LLM agent as the default
    engine after the 2026-06-12 incident where the agent fabricated a SUCCESS
    for an order that was never placed.
    """
    import requests as _requests

    daemon_url = (os.environ.get("DAEMON_INTERNAL_URL") or "http://daemon:5002").rstrip("/")
    expiry_mmyy = f"{int(payment_card['card_expiry_month']):02d}{str(payment_card['card_expiry_year'])[-2:]}"

    primary_addr = next((a for a in org.addresses if a.get("is_primary")), None) \
                   or (org.addresses[0] if org.addresses else {})
    warehouse_address = (
        primary_addr.get("address1")
        or primary_addr.get("address")
        or primary_addr.get("address_line")
        or " ".join(p for p in (
            primary_addr.get("street_line1"),
            primary_addr.get("street_line2"),
            primary_addr.get("city"),
            primary_addr.get("state"),
            primary_addr.get("zip_code"),
        ) if p)
        or ""
    )

    name_on_card = payment_card.get("card_holder_name") or org.contact_name or org.org_name
    name_parts = (name_on_card or "").split()
    billing = dict(org.billing_address or {})
    if not billing.get("postcode"):
        billing["postcode"] = (payment_card.get("billing_zip") or "").strip()
    billing.setdefault("firstname", name_parts[0] if name_parts else "")
    billing.setdefault("lastname", " ".join(name_parts[1:]) if len(name_parts) > 1 else "")
    billing.setdefault("telephone", org.phone_number or "")

    answers = dict(org.checkout_answers or {})
    answers.setdefault("warehouse_address", warehouse_address)

    org_config = {
        "name":              org.org_name,
        "good360_email":     org.good360_email,
        "good360_password":  org.good360_password,
        "card": {
            "name":   name_on_card,
            "number": payment_card["card_number"],
            "expiry": expiry_mmyy,
            "cvv":    payment_card.get("card_cvv"),
            "type":   payment_card.get("card_type") or "visa",
        },
        "billing_address":   billing,
        "checkout_answers":  answers,
        "warehouse_address": warehouse_address,
        "contact_name":      org.contact_name or "",
        "contact_phone":     org.phone_number or "",
    }

    logger.info("[DAEMON] dispatching deterministic checkout · org=%s truck=%s card=****%s",
                org.org_id, truck.truck_event_id, str(payment_card["card_number"])[-4:])
    try:
        resp = _requests.post(f"{daemon_url}/test_checkout", json={
            "org_key": f"qb_{org.org_id}",
            "org_config": org_config,
            "truck_name": truck.truck_title,
            "truck_url": truck.truck_url,
            "force_login": True,
        }, timeout=420)
        payload = resp.json()
    except Exception as exc:  # noqa: BLE001
        return CheckoutResult(
            success=False, status="failed_checkout", mode="auto_buy",
            error_message=f"[DAEMON] checkout dispatch failed: {type(exc).__name__}: {exc}")

    d_status = (payload.get("status") or "").upper()
    d_msg = payload.get("message") or ""
    capture = payload.get("capture_path") or ""
    suffix = f" (capture: {capture})" if capture else ""

    if d_status == "SUCCESS":
        # The daemon's SUCCESS is page-evidence-based (success URL, order-#
        # regex, or explicit thank-you text) and screenshotted. Prefer the
        # extracted order number; fall back to the evidence detail string.
        import re as _re
        m = _re.search(r"#=\s*([^,)]+)", d_msg)
        conf = (m.group(1).strip() if m else "") or d_msg[:140]
        return CheckoutResult(
            success=True, status="success", mode="auto_buy",
            confirmation_number=conf,
            order_total=float(truck.truck_price) if truck.truck_price else None,
            error_message=None)
    if d_status == "CARD_DECLINED":
        return CheckoutResult(success=False, status="failed_checkout", mode="auto_buy",
                              error_message=f"[DAEMON] card declined: {d_msg}{suffix}")
    if d_status == "MISSED":
        return CheckoutResult(success=False, status="truck_gone", mode="auto_buy",
                              error_message=f"[DAEMON] {d_msg}{suffix}")
    if d_status == "MANUAL":
        return CheckoutResult(success=False, status="alerted_manual", mode="auto_buy",
                              error_message=f"[DAEMON] needs review: {d_msg}{suffix}")
    return CheckoutResult(success=False, status="failed_checkout", mode="auto_buy",
                          error_message=f"[DAEMON] {d_status or 'error'}: {d_msg}{suffix}")


def run_checkout_sequence(org: OrgContext, truck: TruckContext,
                          payment_card: dict,
                          *, test_mode: bool = False) -> CheckoutResult:
    """
    Execute full checkout sequence using browser_agent (Playwright).
    1. Open Good360
    2. Logout (if logged in)
    3. Login as org
    4. Navigate to truck URL
    5. Answer checkout questions
    6. Enter payment card details
    7. Submit order
    8. Capture confirmation

    Returns CheckoutResult.

    Dry-run gate: when QUICKBEED_DRY_RUN=true (env var) AND the org came from
    QuickBeed, we DO NOT click Place Order. We log the masked checkout
    payload and return a synthetic success-shaped result so the rest of the
    flow exercises end-to-end without spending real money. Required for the
    first staging round per ops policy.
    """
    # In test_mode the caller has already installed a fake card and suppressed
    # production side effects, so the dry-run gate is irrelevant — let the
    # agent click Place Order so we observe the real card-decline response.
    dry_run = os.environ.get("QUICKBEED_DRY_RUN", "").lower() in ("1", "true", "yes")
    is_qb = getattr(org, "good360_org_id", "sentinel") is None  # QB orgs have None here
    if dry_run and is_qb and not test_mode:
        masked_card = (payment_card.get("card_number") or "")[-4:]
        logger.warning(
            "🛑 QUICKBEED_DRY_RUN active — would attempt purchase\n"
            f"   org={org.org_name} ({org.org_id}) · truck={truck.truck_title!r} @ ${truck.truck_price}\n"
            f"   card=****{masked_card} exp={payment_card.get('card_expiry_month'):02d}/{payment_card.get('card_expiry_year')}\n"
            f"   good360_email={org.good360_email}"
        )
        return CheckoutResult(
            success=True,
            status="dry_run_ok",
            mode="auto_buy",
            order_total=truck.truck_price,
            confirmation_number=f"DRYRUN-{org.org_id}-{truck.truck_event_id}",
            error_message=None,
        )

    # Engine selection. Default is the deterministic daemon/Playwright
    # executor — after 2026-06-12, when the LLM devtools agent fabricated
    # refusals AND a success-for-an-order-that-never-existed, the LLM was
    # demoted to an explicit opt-in via AUTOBUY_ENGINE=devtools_agent.
    engine = (os.environ.get("AUTOBUY_ENGINE") or "daemon").strip().lower()
    if engine != "devtools_agent":
        return _run_checkout_via_daemon(org, truck, payment_card, test_mode=test_mode)

    # LLM agent path (opt-in). If MCP is unavailable we surface that as a
    # failed_checkout with a clear error_message so the operator notices.
    try:
        return _run_checkout_via_devtools_mcp(org, truck, payment_card, test_mode=test_mode)
    except _DevToolsMCPUnavailable as exc:
        logger.error("DevTools MCP unavailable: %s", exc)
        return CheckoutResult(
            success=False, status="failed_checkout", mode="auto_buy",
            error_message=f"[MCP] DevTools agent unavailable: {exc} "
                          f"(check that openai-agents + chrome-devtools-mcp "
                          f"are installed and good360_devtools_agent is "
                          f"importable)",
        )


# ─── Alert-Only Flow ─────────────────────────────────────────────────────────
def handle_alert_only(org: OrgContext, truck: TruckContext) -> CheckoutResult:
    """
    Alert-only mode: no purchase attempted.
    Send email/SMS alert and log as alerted_manual.
    """
    from notifier import notify_truck_alert, start_dispatch_worker

    start_dispatch_worker()

    expiry_minutes = int(get_config("alert_only_expiry_minutes", "30"))

    notify_truck_alert(
        org_id=org.org_id,
        truck_event_id=truck.truck_event_id,
        truck_title=truck.truck_title,
        truck_price=truck.truck_price,
        truck_url=truck.truck_url,
        truck_location=truck.truck_location,
        truck_category=truck.truck_category,
        alert_mode="alert_only",
        expiry_minutes=expiry_minutes,
    )

    log_system_event("alert_sent", "info", org.org_id,
                     f"Alert-only notification sent for truck {truck.truck_event_id}")

    return CheckoutResult(
        success=False,
        status="alerted_manual",
        mode="alert_only",
        error_message="Alert sent — manual purchase required",
    )


# ─── Master Card Fallback ─────────────────────────────────────────────────────
def check_master_card_available() -> bool:
    """Check if master card fallback is enabled and consented."""
    return get_config("master_card_enabled", "1") == "1"


def get_master_payment_method() -> dict | None:
    """Get active system admin payment method."""
    with get_db_connection() as conn:
        row = conn.execute(
            """SELECT * FROM system_payment_methods
               WHERE is_active=1 ORDER BY priority ASC LIMIT 1"""
        ).fetchone()
    if not row:
        return None

    with _get_vault() as decrypt:
        return {
            "id": row["id"],
            "card_label": row["card_label"],
            "card_holder_name": row["card_holder_name"],
            "card_number": decrypt(row["card_number_enc"]),
            "card_last4": row["card_last4"],
            "card_expiry_month": row["card_expiry_month"],
            "card_expiry_year": row["card_expiry_year"],
            "card_cvv": decrypt(row["card_cvv_enc"]),
            "billing_zip": row["billing_zip"],
            "billing_address": row["billing_address"],
            "card_type": row["card_type"],
        }


def check_master_card_consent(org_id: int) -> bool:
    """Check org has signed master_card_consent agreement."""
    with get_db_connection() as conn:
        row = conn.execute(
            """SELECT 1 FROM agreements
               WHERE nonprofit_id=? AND agreement_type='master_card_consent'
               AND is_active=1""",
            (org_id,)
        ).fetchone()
    return row is not None


def handle_master_card_fallback(org: OrgContext, truck: TruckContext,
                                failed_payment_ids: list[int]) -> CheckoutResult:
    """
    Execute master card fallback purchase.
    Requires: master_card_enabled=1 + org.master_card_consent signed.
    """
    if not check_master_card_available():
        return CheckoutResult(
            success=False,
            status="failed_payment",
            mode="master_card_fallback",
            error_message="Master card fallback disabled",
        )

    if not check_master_card_consent(org.org_id):
        return CheckoutResult(
            success=False,
            status="failed_payment",
            mode="master_card_fallback",
            error_message="Master card consent not signed by org",
        )

    master_card = get_master_payment_method()
    if not master_card:
        return CheckoutResult(
            success=False,
            status="failed_payment",
            mode="master_card_fallback",
            error_message="No system master card available",
        )

    # Run checkout with master card
    result = run_checkout_sequence(org, truck, master_card)
    result.mode = "master_card_fallback"

    if result.success:
        # Record master card transaction + penalty
        penalty_multiplier = float(get_config("master_card_penalty_multiplier", "3.0"))
        finding_fee = float(get_config("finding_fee_usd", "500"))
        penalty_amount = finding_fee * penalty_multiplier

        with get_db_connection() as conn:
            # Get purchase attempt id
            pa_row = conn.execute(
                """SELECT id FROM purchase_attempts
                   WHERE nonprofit_id=? AND truck_event_id=?
                   ORDER BY id DESC LIMIT 1""",
                (org.org_id, truck.truck_event_id)
            ).fetchone()
            pa_id = pa_row["id"] if pa_row else None

            import uuid as uuid_lib
            invoice_number = f"INV-{datetime.utcnow().strftime('%Y%m%d')}-{uuid_lib.uuid4().hex[:6].upper()}"

            conn.execute(
                """INSERT INTO master_card_transactions
                   (purchase_attempt_id, nonprofit_id, system_payment_id,
                    truck_price, penalty_multiplier, penalty_amount,
                    total_org_owes, invoice_number)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (pa_id, org.org_id, master_card["id"],
                 truck.truck_price, penalty_multiplier,
                 penalty_amount, truck.truck_price + penalty_amount,
                 invoice_number)
            )

            # Update system card used
            conn.execute(
                """UPDATE system_payment_methods
                   SET total_charged = total_charged + ?,
                       last_used = datetime('now')
                   WHERE id = ?""",
                (result.order_total or truck.truck_price, master_card["id"])
            )
            conn.commit()

        # Send admin + org alerts
        from notifier import notify_admin_alert, notify_master_card_used, start_dispatch_worker
        start_dispatch_worker()

        notify_master_card_used(
            org_id=org.org_id,
            purchase_attempt_id=pa_id,
            truck_price=truck.truck_price,
            penalty_amount=penalty_amount,
            invoice_number=invoice_number,
        )

        notify_admin_alert(
            subject=f"Master Card Fallback Used: {org.org_name}",
            message=f"Org {org.org_name} (ID={org.org_id}) used master card for "
                    f"truck {truck.truck_title} (${truck.truck_price:,.2f}). "
                    f"Penalty: ${penalty_amount:,.2f}. Invoice: {invoice_number}.",
            severity="critical",
            org_id=org.org_id,
        )

        result.status = "success_mastercard"

    return result


# ─── Main Purchase Flow ───────────────────────────────────────────────────────
def _live_purchase_approval_blocker(org_id: int) -> str | None:
    """Defense-in-depth gate for LIVE purchases. Returns a refusal reason,
    or None when the purchase is approved.

    Whatever upstream queue selected this org, the purchase is only allowed
    when the dashboard mirror — the operator's source of truth — says the
    customer is BOTH QuickBeed-active AND toggled into rotation. Added after
    the 2026-06-11 incident where a roster-sync bug let the engine attempt a
    purchase for a customer the operator had removed from rotation.

    FAIL CLOSED: any error verifying approval refuses the purchase.
    """
    try:
        with get_db_connection() as conn:
            row = conn.execute(
                "SELECT quickbeed_customer_id, org_name FROM nonprofits WHERE id = ?",
                (org_id,)).fetchone()
        if not row:
            return f"org {org_id} not found in roster"
        keys = row.keys() if hasattr(row, "keys") else []
        qb_id = row["quickbeed_customer_id"] if "quickbeed_customer_id" in keys else None
        if not qb_id:
            return (f"org {org_id} ({row['org_name']}) has no QuickBeed customer id — "
                    "live purchases are only allowed for roster customers synced from QuickBeed")

        import sys as _sys
        if "/app/missioncontrol" not in _sys.path:
            _sys.path.insert(0, "/app/missioncontrol")
        from db import get_conn as _dash_conn  # type: ignore
        with _dash_conn() as c:
            cust = c.execute(
                "SELECT status, in_rotation, organization_name FROM customers WHERE id = ?",
                (qb_id,)).fetchone()
        if not cust:
            return f"QuickBeed customer {qb_id} not in the dashboard mirror — cannot verify approval"
        name = cust["organization_name"] or qb_id
        if (cust["status"] or "") != "active":
            return f"customer {name!r} has status={cust['status']!r} (not active)"
        if int(cust["in_rotation"] or 0) != 1:
            return f"customer {name!r} is NOT in rotation — operator has not approved autobuy for this account"
        return None
    except Exception as e:  # noqa: BLE001
        return (f"approval check failed ({type(e).__name__}: {e}) — "
                "refusing to purchase (fail-closed)")


def _alert_blocked_purchase(org_name: str, truck_title: str, reason: str) -> None:
    """Telegram the operator that the approval gate refused a purchase.
    A trip of this gate means an upstream selection bug — it must be loud."""
    import html as _html
    import requests as _requests
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_OPERATOR_CHAT_ID") or "").strip()
    msg = ("🚫 <b>PURCHASE BLOCKED — approval gate</b>\n"
           f"Org: <b>{_html.escape(str(org_name))}</b>\n"
           f"Truck: {_html.escape(str(truck_title))}\n"
           f"Reason: {_html.escape(str(reason))}\n"
           "The purchase engine refused to buy for a customer the dashboard "
           "does not approve. Investigate how this org was selected.")
    if token and chat:
        try:
            _requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": msg, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=10)
        except Exception:  # noqa: BLE001
            pass
    try:
        import notifications_log
        notifications_log.record_telegram(
            source="autobuy_approval_gate", message=msg, delivered=bool(token and chat),
            channel="operator", level="error",
            title=f"Purchase blocked: {org_name}")
    except Exception:  # noqa: BLE001
        pass


def attempt_purchase(org_id: int, truck_event_id: int,
                     *, test_card_override: dict | None = None) -> CheckoutResult:
    """
    Main entry point for purchasing a truck for an org.

    Flow:
    1. Load org + truck contexts
    2. Check if auto_buy is ON
       - OFF → alert-only flow → return
    3. Try each payment card (1→2→3)
    4. All cards fail + master_card_fallback=1 + consent → master card fallback
    5. Return CheckoutResult

    test_card_override (optional): a card dict shaped like an entry in
    org.payment_methods. When supplied, the function:
      - tolerates a customer with bad / missing real card data
      - uses the override as the ONLY payment method (no fallbacks)
      - skips cooldown application, billing fee recording, notifier dispatch,
        and master-card fallback — i.e. no production-state side effects.
    This lets the readiness-check / test harness run the exact production
    code path against any customer without polluting real data or charging
    a real card.
    """
    test_mode = test_card_override is not None
    logger.info(f"Starting purchase: org_id={org_id}, truck_event_id={truck_event_id}"
                f"{' [TEST MODE — fake card, side effects suppressed]' if test_mode else ''}")

    # Load contexts. In test mode we tolerate customers whose real card data
    # in QuickBeed is bad — we're going to swap in the override anyway.
    try:
        org = load_org_context(org_id, require_payment_methods=not test_mode)
        truck = load_truck_context(truck_event_id)
    except ValueError as e:
        logger.error(f"Context load failed: {e}")
        return CheckoutResult(success=False, status="failed_checkout",
                             mode="auto_buy", error_message=str(e))

    # HARD GATE: live purchases only for customers the operator has approved
    # (dashboard: status=active AND in_rotation=1). This runs at the point of
    # purchase regardless of which queue selected the org — a selection-layer
    # bug must never be able to reach a checkout. Test mode (fake decline
    # card, operator-initiated, no charge possible) bypasses it.
    if not test_mode:
        blocker = _live_purchase_approval_blocker(org_id)
        if blocker:
            msg = f"PURCHASE BLOCKED by approval gate: {blocker}"
            logger.error(f"{msg} — org_id={org_id}, truck={truck.truck_title!r}")
            _alert_blocked_purchase(org.org_name, truck.truck_title, blocker)
            return CheckoutResult(success=False, status="failed_checkout",
                                  mode="auto_buy", error_message=msg)

    # Mark truck as assigned AND create the matching purchase_attempts row
    # in one transaction. Previously these were two separate connections /
    # commits, so a failure on the INSERT would leave truck_events with
    # status='assigned' but no purchase_attempt — an orphan that confuses
    # the queue manager and the dashboard.
    mode = "auto_buy" if org.auto_buy_global else "alert_only"
    try:
        with get_db_connection() as conn:
            try:
                conn.execute(
                    """UPDATE truck_events
                       SET status='assigned', assigned_to_org_id=?, assigned_at=datetime('now')
                       WHERE id=?""",
                    (org_id, truck_event_id)
                )
                cursor = conn.execute(
                    """INSERT INTO purchase_attempts
                       (truck_event_id, nonprofit_id, payment_method_id, mode, status)
                       VALUES (?, ?, ?, ?, 'in_progress')""",
                    (truck_event_id, org_id, None, mode)
                )
                attempt_id = cursor.lastrowid
                conn.commit()
            except Exception:
                conn.rollback()
                raise
    except Exception as e:
        logger.error(f"Failed to record purchase attempt atomically: {e}")
        return CheckoutResult(success=False, status="failed_checkout",
                              mode=mode, error_message=f"db_write_failed: {e}")

    # Alert-only flow
    if not org.auto_buy_global:
        result = handle_alert_only(org, truck)
        update_purchase_attempt(attempt_id, result.status,
                               error_message=result.error_message)
        return result

    # Auto-buy flow: try each payment card
    if test_mode:
        # Replace real cards with the override. priority/id defaults so the
        # rest of the loop's bookkeeping doesn't crash on missing keys.
        override = dict(test_card_override)
        override.setdefault("id", None)
        override.setdefault("priority", 1)
        override.setdefault("card_last4", str(override.get("card_number", ""))[-4:])
        payment_methods = [override]
    else:
        payment_methods = get_active_payment_methods(org)
        if not payment_methods:
            logger.warning(f"Org {org_id} has no valid payment methods")
            # Fall through to master card check
            payment_methods = []

    # Track the last attempt's result/error so a final "all payment methods
    # failed" return preserves the underlying agent message (otherwise the
    # readiness-check classifier and the dashboard can't tell WHICH stage
    # the run ended on).
    last_result = None
    for card in payment_methods:
        logger.info(f"Attempting checkout with card #{card['priority']} for org {org_id}")

        result = run_checkout_sequence(org, truck, card, test_mode=test_mode)
        result.mode = "auto_buy"
        result.payment_method_id = card["id"]
        last_result = result

        if result.success:
            update_purchase_attempt(
                attempt_id, "success",
                confirmation_number=result.confirmation_number,
                order_total=result.order_total,
                screenshot_path=result.screenshot_path,
                cooldown_applied=not test_mode,
            )

            # Production side effects — skipped in test mode so a readiness
            # check doesn't apply cooldowns, charge finding fees, or dispatch
            # operator notifications.
            if not test_mode:
                from queue_manager import on_purchase_success
                cooldown_until = on_purchase_success(
                    org_id, truck_event_id, result.confirmation_number
                )

                from billing_manager import record_finding_fee
                record_finding_fee(org_id, attempt_id, truck.truck_price)

                from notifier import notify_purchase_success, start_dispatch_worker
                start_dispatch_worker()
                notify_purchase_success(
                    org_id=org_id,
                    truck_event_id=truck_event_id,
                    confirmation_number=result.confirmation_number or "",
                    order_total=result.order_total or truck.truck_price,
                    truck_title=truck.truck_title,
                )

            logger.info(f"SUCCESS: org={org_id} truck={truck_event_id} "
                       f"conf={result.confirmation_number}"
                       f"{' [test mode]' if test_mode else ''}")
            return result

        else:
            # Card declined / checkout failed
            if card.get("id") is not None:
                record_card_decline(card["id"])
            update_purchase_attempt(
                attempt_id,
                status="failed_payment" if result.status == "failed_checkout" else result.status,
                error_message=result.error_message,
                screenshot_path=result.screenshot_path,
            )
            logger.warning(f"Card #{card['priority']} failed: {result.error_message}")

    # All cards failed — try master card fallback (production only)
    if not test_mode and org.master_card_fallback:
        logger.info(f"All org cards failed — attempting master card fallback for org {org_id}")
        master_result = handle_master_card_fallback(org, truck,
                                                    [c["id"] for c in payment_methods])
        master_result.attempt_number = len(payment_methods) + 1

        if master_result.success:
            update_purchase_attempt(
                attempt_id, master_result.status,
                confirmation_number=master_result.confirmation_number,
                order_total=master_result.order_total,
                screenshot_path=master_result.screenshot_path,
                cooldown_applied=True,
            )

            from queue_manager import on_purchase_success
            on_purchase_success(org_id, truck_event_id,
                              master_result.confirmation_number)

            return master_result

    # Complete failure. Surface the most informative error we saw:
    # the last attempt's underlying agent message if any, else a generic
    # "no payment methods attempted" so the operator knows the loop never
    # ran a card.
    final_err = (last_result.error_message
                 if (last_result and last_result.error_message)
                 else "All payment methods failed (no payment methods attempted)")
    update_purchase_attempt(attempt_id, "failed_payment", error_message=final_err)

    with get_db_connection() as conn:
        conn.execute(
            """UPDATE truck_events
               SET status='missed' WHERE id=?""",
            (truck_event_id,)
        )
        conn.commit()

    return CheckoutResult(
        success=False,
        status="failed_payment",
        mode="auto_buy",
        error_message=final_err,
    )


# ─── Verify Credentials ───────────────────────────────────────────────────────
def verify_org_credentials(org_id: int) -> tuple[bool, str]:
    """
    Attempt to log into Good360 with org's credentials.
    Returns (success, message).
    """
    try:
        org = load_org_context(org_id)
    except ValueError as e:
        return False, str(e)

    # Use browser agent to test login
    from browser_agent import browser_agent as _browser_agent_call

    login_message = f"""
Test Good360 login for org: {org.org_name}

Steps:
1. Open https://www.good360.org
2. If logged in, logout first
3. Attempt login with:
   Email: {org.good360_email}
   Password: {org.good360_password}
4. Report login success/failure
5. End task with result as JSON:
   {{"success": true/false, "message": "..."}}
"""

    try:
        response = _browser_agent_call(message=login_message, reset=True)
        response_text = str(response)

        import re
        json_match = re.search(r'\{[^{}]*"success"[^{}]*\}', response_text, re.DOTALL)
        if json_match:
            result = json.loads(json_match.group())
            success = result.get("success", False)
            msg = result.get("message", "")
        else:
            success = "logged in" in response_text.lower() or "login successful" in response_text.lower()
            msg = response_text[:300]

        # Update DB
        with get_db_connection() as conn:
            login_verified = 1 if success else 0
            conn.execute(
                """UPDATE nonprofit_credentials
                   SET login_verified=?, last_login=datetime('now')
                   WHERE nonprofit_id=?""",
                (login_verified, org_id)
            )
            conn.commit()

        return success, msg
    except Exception as e:
        return False, str(e)


# ─── CLI ─────────────────────────────────────────────────────────────────────
def main():
    import argparse
    parser = argparse.ArgumentParser(prog="good360_autobuy_v2.py",
                                     description="Multi-account Good360 checkout")
    sub = parser.add_subparsers(dest="cmd")

    run = sub.add_parser("run", help="Run purchase for an org")
    run.add_argument("org_id", type=int)
    run.add_argument("truck_event_id", type=int)

    verify = sub.add_parser("verify", help="Verify org credentials")
    verify.add_argument("org_id", type=int)

    test = sub.add_parser("test-sequence", help="Test checkout sequence with mock data")
    test.add_argument("org_id", type=int)

    args = parser.parse_args()

    if args.cmd == "run":
        result = attempt_purchase(args.org_id, args.truck_event_id)
        print(json.dumps({
            "success": result.success,
            "status": result.status,
            "mode": result.mode,
            "confirmation": result.confirmation_number,
            "order_total": result.order_total,
            "error": result.error_message,
        }, indent=2))
    elif args.cmd == "verify":
        ok, msg = verify_org_credentials(args.org_id)
        print(f"Login {'SUCCESS' if ok else 'FAILED'}: {msg}")
    elif args.cmd == "test-sequence":
        print("Test sequence — load org context only (no actual browser)")
        try:
            org = load_org_context(args.org_id)
            print(f"  Org: {org.org_name}")
            print(f"  Email: {org.good360_email}")
            print(f"  Cards: {len(org.payment_methods)}")
            print(f"  Addresses: {len(org.addresses)}")
            print(f"  Categories: {list(org.category_prefs.keys())}")
        except Exception as e:
            print(f"Error: {e}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
