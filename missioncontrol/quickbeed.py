"""QuickBeed customer-sync client.

Talks to /api/v1/customers (well, /functions/apiV1Customers — same thing) on the
onboarding app. Keeps a local mirror of profile + ops fields. Does NOT store
partner_credentials or card numbers locally — those are fetched on-demand at
use time and discarded immediately, with a `?reason=` query parameter so the
upstream audit log captures every access.

Settings consumed (all encrypted in the dashboard's settings table):
  QUICKBEED_BASE_URL        full URL up to and including /functions/apiV1Customers
  QUICKBEED_APP_ID          alternative: just the app id, base URL composed from it
  QUICKBEED_API_TOKEN       bearer token (64 hex chars)
  QUICKBEED_WEBHOOK_SECRET  HMAC key for inbound webhook verification
  QUICKBEED_CONSUMER_ID     opaque identifier for support/audit trail
  QUICKBEED_POLL_INTERVAL_SECONDS  default 300

The contract document is the source of truth for request/response shape — see
`QuickBeed Customer Sync API — Exact Contract`.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Any, Iterator

import requests

from db import get_conn
import secrets_store


DEFAULT_BASE_TEMPLATE = "https://app--quickbeed.base44.app/api/apps/{app_id}/functions/apiV1Customers"
DEFAULT_POLL_INTERVAL = 300
PAGE_SIZE = 100  # contract caps at 100; max throughput per request

# `reason` values the contract policy considers valid.
REASON_ROUND_ROBIN = "round_robin_selection"
REASON_CREDENTIAL_USE = "credential_use"
REASON_PAYMENT_PROCESSING = "payment_processing"
REASON_RECONCILIATION = "reconciliation"
REASON_SUPPORT = "support_investigation"


# ============================================================
# Settings access (encrypted DB)
# ============================================================

def _setting(key: str) -> str | None:
    with get_conn() as c:
        row = c.execute("SELECT value_enc FROM settings WHERE key = ?", (key,)).fetchone()
    if not row:
        return None
    try:
        return secrets_store.decrypt(row["value_enc"])
    except Exception:
        return None


def get_config() -> dict:
    """Return a dict of QuickBeed configuration. Raises if mandatory bits missing."""
    token = _setting("QUICKBEED_API_TOKEN")
    if not token:
        raise QuickBeedConfigError("QUICKBEED_API_TOKEN not set")

    base = (_setting("QUICKBEED_BASE_URL") or "").strip().rstrip("/")
    if not base:
        app_id = (_setting("QUICKBEED_APP_ID") or "").strip()
        if not app_id:
            raise QuickBeedConfigError("Set QUICKBEED_BASE_URL or QUICKBEED_APP_ID")
        base = DEFAULT_BASE_TEMPLATE.format(app_id=app_id)

    poll = _setting("QUICKBEED_POLL_INTERVAL_SECONDS")
    try:
        poll_s = int(poll) if poll else DEFAULT_POLL_INTERVAL
    except ValueError:
        poll_s = DEFAULT_POLL_INTERVAL

    return {
        "base_url": base,
        "token": token,
        "webhook_secret": _setting("QUICKBEED_WEBHOOK_SECRET") or "",
        "consumer_id": _setting("QUICKBEED_CONSUMER_ID") or "",
        "poll_interval_s": max(60, poll_s),
    }


class QuickBeedConfigError(RuntimeError):
    """Configuration is incomplete / invalid."""


class QuickBeedHTTPError(RuntimeError):
    def __init__(self, status: int, body: str):
        self.status = status
        self.body = body
        super().__init__(f"QuickBeed HTTP {status}: {body[:200]}")


# ============================================================
# HTTP client
# ============================================================

class QuickBeedClient:
    def __init__(self, *, base_url: str, token: str, timeout: float = 15.0):
        self.base_url = base_url.rstrip("/")
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "good360-monitor/customer-sync 1.0",
        }
        self._timeout = timeout

    @classmethod
    def from_settings(cls) -> "QuickBeedClient":
        cfg = get_config()
        return cls(base_url=cfg["base_url"], token=cfg["token"])

    def _get(self, path: str, params: dict | None = None) -> tuple[dict, dict]:
        url = self.base_url + path
        backoff = 1.0
        for attempt in range(6):
            try:
                resp = requests.get(url, headers=self._headers, params=params, timeout=self._timeout)
            except requests.RequestException as exc:
                if attempt == 5:
                    raise
                time.sleep(min(backoff, 60))
                backoff *= 2
                continue
            if resp.status_code == 429:
                # No Retry-After per the contract — local exponential back-off.
                time.sleep(min(backoff, 60))
                backoff *= 2
                continue
            if resp.status_code >= 500 and attempt < 5:
                time.sleep(min(backoff, 60))
                backoff *= 2
                continue
            if resp.status_code >= 400:
                raise QuickBeedHTTPError(resp.status_code, resp.text)
            return resp.json(), dict(resp.headers)
        raise QuickBeedHTTPError(0, "exhausted retries without a usable response")

    # --- endpoints ---
    #
    # Routing convention (server-side): all sub-routes are dispatched via the
    # `?endpoint=` query parameter on the bare function URL. URL sub-paths
    # are NOT honored — Base44 doesn't forward them to the function.
    #   - list:      no endpoint (default)
    #   - health:    ?endpoint=health
    #   - full rec:  ?endpoint=customers/{id}&reason=...
    #   - status:    ?endpoint=customers/{id}/status

    def health(self) -> dict:
        body, _ = self._get("", params={"endpoint": "health"})
        return body

    def list_customers(
        self,
        *,
        status: str | None = None,
        updated_since: str | None = None,
        page: int = 1,
        page_size: int = PAGE_SIZE,
        reason: str = REASON_RECONCILIATION,
    ) -> tuple[list[dict], dict]:
        params: dict[str, Any] = {"page": page, "page_size": page_size, "reason": reason}
        if status:
            params["status"] = status
        if updated_since:
            params["updated_since"] = updated_since
        body, _ = self._get("", params=params)
        return body.get("data") or [], body.get("pagination") or {}

    def iter_customers(
        self,
        *,
        status: str | None = None,
        updated_since: str | None = None,
        reason: str = REASON_RECONCILIATION,
    ) -> Iterator[dict]:
        page = 1
        while True:
            batch, pag = self.list_customers(
                status=status, updated_since=updated_since,
                page=page, page_size=PAGE_SIZE, reason=reason,
            )
            for rec in batch:
                yield rec
            if not pag.get("has_next"):
                return
            page += 1

    def get_customer(self, customer_id: str, *, reason: str) -> dict:
        """Fetch a full record (creds + cards). Audit-logged on the server."""
        if not reason:
            raise ValueError("reason is required by the contract")
        body, _ = self._get("", params={
            "endpoint": f"customers/{customer_id}",
            "reason": reason,
        })
        return body

    def get_status(self, customer_id: str) -> dict:
        """Cheap eligibility check. No reason required."""
        body, _ = self._get("", params={
            "endpoint": f"customers/{customer_id}/status",
        })
        return body


# ============================================================
# Local mirror
# ============================================================

def _b(v):
    """Coerce a JSON bool/None into 0/1/None for SQLite."""
    if v is None:
        return None
    return 1 if v else 0


def upsert_customer(rec: dict) -> None:
    """Mirror profile + operations fields. Never stores credentials or cards."""
    profile = rec.get("profile") or {}
    ops = rec.get("operations") or {}
    with get_conn() as c:
        c.execute(
            """INSERT INTO customers
                (id, status, status_reason, created_at, updated_at, last_synced_at,
                 full_name, organization_name, email, phone,
                 warehouse_address, has_loading_dock, has_pallet_capability,
                 distribution_method, people_served, preferred_location,
                 open_to_alternatives, truck_selection, priority_level, max_budget)
               VALUES (?,?,?,?,?, datetime('now'), ?,?,?,?, ?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 status = excluded.status,
                 status_reason = excluded.status_reason,
                 created_at = excluded.created_at,
                 updated_at = excluded.updated_at,
                 last_synced_at = datetime('now'),
                 full_name = excluded.full_name,
                 organization_name = excluded.organization_name,
                 email = excluded.email,
                 phone = excluded.phone,
                 warehouse_address = excluded.warehouse_address,
                 has_loading_dock = excluded.has_loading_dock,
                 has_pallet_capability = excluded.has_pallet_capability,
                 distribution_method = excluded.distribution_method,
                 people_served = excluded.people_served,
                 preferred_location = excluded.preferred_location,
                 open_to_alternatives = excluded.open_to_alternatives,
                 truck_selection = excluded.truck_selection,
                 priority_level = excluded.priority_level,
                 max_budget = excluded.max_budget""",
            (
                rec["id"], rec.get("status"), rec.get("status_reason"),
                rec.get("created_at"), rec.get("updated_at"),
                profile.get("full_name"), profile.get("organization_name"),
                profile.get("email"), profile.get("phone"),
                ops.get("warehouse_address"),
                _b(ops.get("has_loading_dock")), _b(ops.get("has_pallet_capability")),
                ops.get("distribution_method"), ops.get("people_served"),
                ops.get("preferred_location"), _b(ops.get("open_to_alternatives")),
                ops.get("truck_selection"), ops.get("priority_level"),
                ops.get("max_budget"),
            ),
        )


def update_status(customer_id: str, status: str, status_reason: str | None = None,
                  updated_at: str | None = None) -> None:
    with get_conn() as c:
        c.execute(
            """UPDATE customers
                  SET status = ?, status_reason = COALESCE(?, status_reason),
                      updated_at = COALESCE(?, updated_at),
                      last_synced_at = datetime('now')
                WHERE id = ?""",
            (status, status_reason, updated_at, customer_id),
        )


def get_sync_state(key: str) -> str | None:
    with get_conn() as c:
        row = c.execute("SELECT value FROM sync_state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_sync_state(key: str, value: str) -> None:
    with get_conn() as c:
        c.execute(
            """INSERT INTO sync_state(key, value) VALUES(?, ?)
               ON CONFLICT(key) DO UPDATE SET
                 value = excluded.value,
                 updated_at = datetime('now')""",
            (key, value),
        )


# ============================================================
# Sync orchestration
# ============================================================

def _push_to_roster() -> dict:
    """After updating dashboard.db, mirror into roster.db.nonprofits so the
    orchestrator's queue manager can schedule QuickBeed customers."""
    try:
        import quickbeed_roster_sync
        return quickbeed_roster_sync.sync_to_roster()
    except Exception:
        # Don't let a roster-sync failure break the dashboard sync. Log it
        # to stderr; the orchestrator can recover on the next pass.
        import traceback
        traceback.print_exc()
        return {"error": "roster sync failed; see container logs"}


def bootstrap() -> dict:
    """Pull every customer once. Use on first install or after a long outage."""
    client = QuickBeedClient.from_settings()
    n = 0
    last_seen: str | None = None
    for rec in client.iter_customers(reason=REASON_RECONCILIATION):
        upsert_customer(rec)
        n += 1
        if rec.get("updated_at"):
            if last_seen is None or rec["updated_at"] > last_seen:
                last_seen = rec["updated_at"]
    if last_seen:
        set_sync_state("last_updated_at", last_seen)
    set_sync_state("last_bootstrap_at", _utcnow_iso())
    return {"synced": n, "last_updated_at": last_seen, "roster": _push_to_roster()}


def incremental_sync() -> dict:
    """Pull anything updated since the last cursor. Safe to call on a timer."""
    cursor = get_sync_state("last_updated_at")
    client = QuickBeedClient.from_settings()
    n = 0
    last_seen = cursor
    # Contract: updated_since is INCLUSIVE. To avoid reprocessing the cursor row,
    # we'd typically use the millisecond after — but the API uses second
    # precision so we simply skip records whose updated_at == cursor exactly
    # AFTER processing. Easier: store the highest updated_at we've seen; pass it
    # as the next `since`. Will reprocess one row each call but upsert is idempotent.
    for rec in client.iter_customers(updated_since=cursor, reason=REASON_RECONCILIATION):
        upsert_customer(rec)
        n += 1
        if rec.get("updated_at") and (last_seen is None or rec["updated_at"] > last_seen):
            last_seen = rec["updated_at"]
    if last_seen:
        set_sync_state("last_updated_at", last_seen)
    set_sync_state("last_incremental_at", _utcnow_iso())
    return {"synced": n, "since": cursor, "last_updated_at": last_seen,
            "roster": _push_to_roster()}


def fetch_full(customer_id: str, *, reason: str) -> dict:
    """Fetch a full record (with credentials + cards) for one-shot use.

    Caller must NOT cache the credentials/cards. Use them and discard.
    """
    client = QuickBeedClient.from_settings()
    rec = client.get_customer(customer_id, reason=reason)
    # Mirror the non-sensitive fields while we have them in hand.
    upsert_customer(rec)
    return rec


def fetch_status_live(customer_id: str) -> dict:
    """Fast eligibility check that bypasses the local mirror."""
    client = QuickBeedClient.from_settings()
    return client.get_status(customer_id)


def list_eligible_customers() -> list[dict]:
    """Customers that pass both the upstream `active` status and the local
    in_rotation flag, with no active local cooldown. Round-robin uses this."""
    with get_conn() as c:
        rows = c.execute(
            """SELECT * FROM customers
                WHERE status = 'active'
                  AND in_rotation = 1
                  AND (cooldown_until IS NULL OR cooldown_until < datetime('now'))
                ORDER BY COALESCE(last_used_at, '1970-01-01') ASC, id ASC"""
        ).fetchall()
    return [dict(r) for r in rows]


def select_next_round_robin() -> dict | None:
    """Pick the next eligible customer (least-recently-used). Stamps last_used_at."""
    candidates = list_eligible_customers()
    if not candidates:
        return None
    pick = candidates[0]
    with get_conn() as c:
        c.execute(
            "UPDATE customers SET last_used_at = datetime('now') WHERE id = ?",
            (pick["id"],),
        )
    return pick


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
