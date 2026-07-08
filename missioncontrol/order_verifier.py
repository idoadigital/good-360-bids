"""Order verifier — syncs Good360 Order History into purchase_attempts.

Spec: docs/superpowers/specs/2026-06-12-dynamic-buyer-history-design.md

Gentle by design: reuses the daemon's saved Playwright sessions
(workdir/browser_data/qb_<org_id>/storage_state.json) and NEVER attempts a
fresh credential login — a dead session alerts the operator and skips
(Good360 lockouts re-trigger easily; learned 2026-06-12).

The pure half (sync_rows) is unit-tested; the Playwright half is exercised
live (verify_customer).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from pathlib import Path

logger = logging.getLogger("order_verifier")

ROSTER_DB = os.environ.get("ROSTER_DB_PATH", "/app/good360_roster/db/roster.db")
def _notifications_enabled() -> bool:
    """ENABLE_NOTIFICATIONS env master switch (false in staging/feature).
    feature_flags lives in the repo root — one level above this package."""
    try:
        import sys
        _root = str(Path(__file__).resolve().parent.parent)
        if _root not in sys.path:
            sys.path.insert(0, _root)
        import feature_flags
        return feature_flags.notifications_enabled()
    except Exception:
        return os.environ.get("ENABLE_NOTIFICATIONS", "true").strip().lower() not in (
            "false", "0", "no", "off")


WORKDIR = os.environ.get("WORKDIR", "/app/workdir")
ORDERS_URL = "https://catalog.good360.org/marketplace/my-account/orders"
STATE_FILE = f"{WORKDIR}/order_verifier_state.json"
LOCK_FILE = f"{WORKDIR}/order_verifier.lock"
LOCK_STALE_S = 15 * 60


# ---------------------------------------------------------------------------
# Pure half — no browser, unit-testable
# ---------------------------------------------------------------------------

def _days_apart(iso_a: str | None, iso_b: str | None) -> int:
    """Whole days between two YYYY-MM-DD strings; huge on parse failure so
    a malformed date can never satisfy a skew threshold."""
    from datetime import date
    try:
        return abs((date.fromisoformat(iso_a) - date.fromisoformat(iso_b)).days)
    except (TypeError, ValueError):
        return 10**6

def sync_rows(org_id: int, site_rows: list[dict],
              verify_screenshot: str | None = None,
              roster_db: str | None = None) -> int:
    """Apply Order History rows to this org's purchase_attempts.

    site_rows: [{order_id, status, admin_fee, date}, ...] as scraped.
    Matches on confirmation_number == order_id. A row whose
    order_status_source='manual' is NEVER overwritten (operator wins —
    the site doesn't know about cancellation phone calls).
    Returns the number of rows updated.
    """
    db = roster_db or ROSTER_DB
    by_order = {str(r["order_id"]): r for r in site_rows if r.get("order_id")}
    if not by_order:
        return 0
    updated = 0
    conn = sqlite3.connect(db, timeout=10.0)
    conn.row_factory = sqlite3.Row
    try:
        attempts = conn.execute(
            """SELECT id, confirmation_number, order_total, order_status,
                      order_status_source, proof, completed_at
               FROM purchase_attempts
               WHERE nonprofit_id = ? AND status = 'success'
                 AND confirmation_number IS NOT NULL""",
            (org_id,)).fetchall()

        def _apply(a, site, matched_by):
            nonlocal updated
            new_status = str(site.get("status") or "").strip().lower() or None
            proof = json.loads(a["proof"] or "{}")
            proof.setdefault("order_id", str(site["order_id"]))
            if matched_by == "date":
                # Keep the checkout evidence string the daemon recorded —
                # the site's order id replaces it as confirmation_number.
                proof.setdefault("checkout_evidence", str(a["confirmation_number"]))
            proof.setdefault("verifications", []).append({
                "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
                "status": new_status,
                "admin_fee": site.get("admin_fee"),
                "screenshot": verify_screenshot,
                "matched_by": matched_by,
            })
            conn.execute(
                """UPDATE purchase_attempts
                   SET order_status = ?,
                       order_status_source = 'auto',
                       order_status_updated_at = datetime('now'),
                       order_total = COALESCE(?, order_total),
                       confirmation_number = ?,
                       proof = ?
                   WHERE id = ?""",
                (new_status, site.get("admin_fee"), str(site["order_id"]),
                 json.dumps(proof), a["id"]))
            updated += 1

        claimed_orders, matched_ids = set(), set()
        for a in attempts:
            site = by_order.get(str(a["confirmation_number"]))
            if site is None:
                continue
            matched_ids.add(a["id"])
            claimed_orders.add(str(site["order_id"]))
            if (a["order_status_source"] or "") == "manual":
                continue  # operator's word beats the site's
            _apply(a, site, "order_id")

        # Fallback: daemon-path successes store an evidence string
        # ("Order placed (url=..., thank-you-message)") instead of the
        # site's order number, so they can never exact-match. Pair each
        # such row with a leftover Order History row by date — but only
        # when the pairing is unambiguous (exactly one attempt and one
        # site row on that date for this org). Site dates are MM/DD/YYYY;
        # a second pass allows ±1 day for midnight/timezone skew, still
        # requiring a unique 1:1 pairing.
        def _site_date(r):
            try:
                mm, dd, yyyy = str(r.get("date") or "").split("/")
                return f"{yyyy}-{mm}-{dd}"
            except ValueError:
                return None

        pending = [a for a in attempts
                   if a["id"] not in matched_ids
                   and (a["order_status_source"] or "") != "manual"
                   and not str(a["confirmation_number"]).startswith("DRYRUN")
                   and not str(a["confirmation_number"]).isdigit()
                   and a["completed_at"]]
        leftovers = [r for r in site_rows
                     if str(r.get("order_id")) not in claimed_orders
                     and _site_date(r)]

        for max_skew in (0, 1):
            for a in list(pending):
                a_date = str(a["completed_at"])[:10]
                cands = [r for r in leftovers if _days_apart(_site_date(r), a_date) <= max_skew]
                if len(cands) != 1:
                    continue
                site = cands[0]
                # The site row must also have exactly one attempt in range,
                # or two purchases on nearby days could race for it.
                contenders = [p for p in pending
                              if p["id"] != a["id"]
                              and _days_apart(_site_date(site),
                                              str(p["completed_at"])[:10]) <= max_skew]
                if contenders:
                    continue
                _apply(a, site, "date")
                pending.remove(a)
                leftovers.remove(site)
                logger.info(f"order sync: org {org_id} attempt {a['id']} "
                            f"date-matched to order {site['order_id']} "
                            f"(skew<={max_skew}d)")
        conn.commit()
    finally:
        conn.close()
    if updated:
        logger.info(f"order sync: org {org_id} — {updated} row(s) updated")
    return updated


def _load_state() -> dict:
    try:
        return json.load(open(STATE_FILE))
    except Exception:  # noqa: BLE001
        return {}


def _save_state(state: dict) -> None:
    json.dump(state, open(STATE_FILE, "w"), indent=1)


def _acquire_lock() -> bool:
    try:
        if os.path.exists(LOCK_FILE):
            if time.time() - os.path.getmtime(LOCK_FILE) < LOCK_STALE_S:
                return False
            os.remove(LOCK_FILE)  # stale lock from a crashed run
        Path(LOCK_FILE).write_text(str(os.getpid()))
        return True
    except Exception:  # noqa: BLE001
        return False


def _release_lock() -> None:
    try:
        os.remove(LOCK_FILE)
    except Exception:  # noqa: BLE001
        pass


def _alert_operator(text: str) -> None:
    """Best-effort Telegram to the operator chat."""
    if not _notifications_enabled():
        return
    import requests
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_OPERATOR_CHAT_ID") or "").strip()
    if not (token and chat):
        return
    try:
        requests.post(f"https://api.telegram.org/bot{token}/sendMessage",
                      json={"chat_id": chat, "text": text}, timeout=10)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Browser half — live-tested
# ---------------------------------------------------------------------------

def _parse_orders_page(page) -> list[dict]:
    """Scrape the Order History table into [{order_id, status, admin_fee,
    date}]. The page renders rows as repeated cell sequences:
    Date / Order ID / Admin fee / FMV / Order status / Location / ..."""
    import re
    txt = page.inner_text("body")
    lines = [l.strip() for l in txt.splitlines() if l.strip()]
    rows, i = [], 0
    while i < len(lines) - 4:
        if re.fullmatch(r"\d{2}/\d{2}/\d{4}", lines[i]) and \
           re.fullmatch(r"\d{6,}", lines[i + 1]):
            fee = None
            m = re.search(r"[\d,]+\.?\d*", lines[i + 2])
            if m:
                fee = float(m.group(0).replace(",", ""))
            rows.append({"date": lines[i], "order_id": lines[i + 1],
                         "admin_fee": fee, "status": lines[i + 4]})
            i += 5
        else:
            i += 1
    return rows


def verify_customer(org_id: int, org_key: str | None = None) -> dict:
    """Verify one customer's orders using their saved browser session.
    Returns {ok, updated, reason}."""
    org_key = org_key or f"qb_{org_id}"
    storage = f"{WORKDIR}/browser_data/{org_key}/storage_state.json"
    if not os.path.exists(storage):
        return {"ok": False, "updated": 0, "reason": "no saved session"}
    from playwright.sync_api import sync_playwright
    shot = f"{WORKDIR}/browser_screenshots/verify_{org_key}_{int(time.time())}.png"
    try:
        with sync_playwright() as pw:
            b = pw.chromium.launch(headless=True)
            ctx = b.new_context(storage_state=storage)
            page = ctx.new_page()
            page.goto(ORDERS_URL, timeout=45000)
            page.wait_for_timeout(6000)
            body = page.inner_text("body")
            if "Order History" not in body or "Sign Out" not in body:
                b.close()
                _alert_operator(f"⚠️ Order verifier: saved session for org "
                                f"{org_id} ({org_key}) is no longer valid — "
                                "skipped (no login attempted). It will renew "
                                "on the next purchase or readiness check.")
                return {"ok": False, "updated": 0, "reason": "session expired"}
            page.screenshot(path=shot, full_page=True)
            site_rows = _parse_orders_page(page)
            b.close()
    except Exception as e:  # noqa: BLE001
        logger.error(f"order verifier browser failure for org {org_id}: {e}")
        return {"ok": False, "updated": 0, "reason": f"browser error: {e}"}
    updated = sync_rows(org_id, site_rows, verify_screenshot=shot)
    state = _load_state()
    state.setdefault("customers", {})[str(org_id)] = {
        "at": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime()),
        "updated": updated, "rows_seen": len(site_rows)}
    _save_state(state)
    return {"ok": True, "updated": updated, "reason": f"{len(site_rows)} site rows"}


def run_full_sync(max_age_hours: float = 24.0, force: bool = False) -> dict:
    """Daily entry point (called from the monitor loop). Verifies every org
    with a recent successful purchase. Time-gated + locked so concurrent
    callers (gunicorn workers, monitor) can't stampede."""
    state = _load_state()
    last = state.get("last_full_run_ts", 0)
    if not force and time.time() - last < max_age_hours * 3600:
        return {"ok": True, "skipped": "ran recently"}
    if not _acquire_lock():
        return {"ok": False, "skipped": "locked"}
    try:
        conn = sqlite3.connect(ROSTER_DB, timeout=10.0)
        org_ids = [r[0] for r in conn.execute(
            """SELECT DISTINCT nonprofit_id FROM purchase_attempts
               WHERE status='success' AND confirmation_number IS NOT NULL
                 AND started_at >= datetime('now', '-90 day')""")]
        conn.close()
        results = {oid: verify_customer(oid) for oid in org_ids}
        state = _load_state()
        state["last_full_run_ts"] = time.time()
        _save_state(state)
        return {"ok": True, "results": {str(k): v for k, v in results.items()}}
    finally:
        _release_lock()
