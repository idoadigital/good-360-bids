"""Customer data readiness — validate and flag incomplete QuickBeed records.

Validates that a full QuickBeed customer record contains every field the
autobuy/checkout path consumes, BEFORE a truck is on the line, and flags
the account in the local mirror (customers.data_ok / data_issues) so the
operator can fix the record while there's still time.

Born from the 2026-05-18 → 06-11 outage: a card expiry stored as
exp_month=1030 left the expiry unparseable, autobuy refused every purchase,
and nobody noticed for three weeks because the failure only surfaced at
purchase time. The checks here mirror what the consumers actually require:

  - internal_org_config / autobuy_v2 context load (creds + usable card)
  - the daemon's checkout form fill (billing address, phone, names)
  - the checkout wizard questions (people served, distribution, warehouse)
  - roster matching (truck_selection) and the price cap (max_budget)

Per [[feedback-customer-data-only]] none of these fields have hardcoded
fallbacks, so a missing field is a guaranteed checkout failure — which is
exactly why every one of them is validated here instead.

Flags are refreshed three ways (see server_v2 poll loop):
  1. opportunistically on every quickbeed.fetch_full() — purchase time,
     test buys, org-config calls;
  2. within one poll interval (~5 min) of a customer's record changing;
  3. a full sweep of active customers at least once a day.
"""
from __future__ import annotations

import html
import json
import os
import re
import traceback
from datetime import datetime, timezone

import requests

from db import get_conn

SWEEP_STATE_KEY = "last_readiness_sweep_at"
SWEEP_INTERVAL_S = 24 * 3600
SWEEP_STATUSES = ("active", "onboarding")


# ============================================================
# Expiry normalization — single source of truth, shared with
# admin_routes._card_to_org so validation can never drift from
# what autobuy actually parses.
# ============================================================

def expiry_mmyy(em, ey) -> str:
    """Normalize QuickBeed expiry fields to the MMYY string autobuy expects.

    QuickBeed records arrive in two shapes: the canonical one
    (exp_month=10, exp_year=2030) and a packed one where the customer's
    "MM/YY" entry was collapsed into the month field (exp_month=1030,
    exp_year=None). Returns "" when the stored value can't be read
    unambiguously — never guesses a date.
    """
    def _digits(v):
        return "".join(ch for ch in str(v) if ch.isdigit()) if v is not None else ""

    md, yd = _digits(em), _digits(ey)
    if not yd and len(md) in (3, 4):
        md, yd = md[:-2], md[-2:]
    if not md or not yd:
        return ""
    month, year = int(md), int(yd) % 100
    if not 1 <= month <= 12:
        return ""
    return f"{month:02d}{year:02d}"


# ============================================================
# Validation
# ============================================================

def _luhn_valid(number: str) -> bool:
    digits = [int(c) for c in number if c.isdigit()]
    if not digits:
        return False
    odd, even = digits[-1::-2], digits[-2::-2]
    return (sum(odd) + sum(sum(divmod(2 * x, 10)) for x in even)) % 10 == 0


def _check_card(pm: dict, idx: int, profile: dict) -> list[str]:
    """Return the list of problems that make this one card unusable."""
    last4 = (pm.get("card_number") or "")[-4:] or "????"
    label = f"card #{idx} (****{last4})"
    probs = []
    number = "".join(c for c in (pm.get("card_number") or "") if c.isdigit())
    if not number:
        probs.append(f"{label}: card number missing")
    elif not (13 <= len(number) <= 19) or not _luhn_valid(number):
        # Found 2026-06-12: Hope 4 Humanity's stored "card" was an 18-digit
        # non-Luhn placeholder; Good360's payment processor rejected it as
        # "card number is invalid" at Place Order. Catch it at intake time.
        probs.append(
            f"{label}: card number is not a valid card "
            f"({len(number)} digits, checksum {'ok' if _luhn_valid(number) else 'fails'}) — "
            "looks like a placeholder; the payment processor will reject it")
    exp = expiry_mmyy(pm.get("exp_month"), pm.get("exp_year"))
    if not exp:
        probs.append(
            f"{label}: expiry unreadable "
            f"(exp_month={pm.get('exp_month')!r}, exp_year={pm.get('exp_year')!r})"
        )
    else:
        month, year = int(exp[:2]), 2000 + int(exp[2:])
        now = datetime.now(timezone.utc)
        if (year, month) < (now.year, now.month):
            probs.append(f"{label}: card expired {month:02d}/{year}")
    if not pm.get("cvv"):
        probs.append(f"{label}: CVV missing")
    if not (pm.get("name_on_card") or profile.get("full_name")):
        probs.append(f"{label}: no name on card (and no profile full name to fall back on)")
    billing = pm.get("billing_address") or {}
    for field in ("street", "city", "state", "zip"):
        if not billing.get(field):
            probs.append(f"{label}: billing {field} missing")
    return probs


def validate_record(rec: dict) -> dict:
    """Validate a FULL QuickBeed record (creds + cards included).

    Returns {"ok": bool, "blockers": [...], "warnings": [...]}.
    blockers = autobuy/checkout will certainly fail; warnings = degraded
    (broken fallback card, no alert email, ...) but a purchase can succeed.
    """
    profile = rec.get("profile") or {}
    pc = rec.get("partner_credentials") or {}
    ops = rec.get("operations") or {}
    cards = rec.get("payment_methods") or []

    blockers: list[str] = []
    warnings: list[str] = []

    # Good360 login — autobuy refuses without both. The email must also be
    # shaped like an email AFTER trimming (consumers strip whitespace; a
    # malformed address is rejected by Good360's sign-in form before submit).
    g360_user = (pc.get("username") or "").strip()
    if not g360_user:
        blockers.append("Good360 login email missing")
    elif not re.match(r"^\S+@\S+\.\S+$", g360_user):
        blockers.append(f"Good360 login email malformed ({g360_user!r}) — sign-in form will reject it")
    if not pc.get("password"):
        blockers.append("Good360 password missing")

    # Payment — autobuy needs at least one fully usable card.
    if not cards:
        blockers.append("no payment method on file")
    else:
        card_problems: list[list[str]] = [
            _check_card(pm, i, profile) for i, pm in enumerate(cards, 1)
        ]
        usable = sum(1 for probs in card_problems if not probs)
        flat = [p for probs in card_problems for p in probs]
        if usable == 0:
            blockers.append("no usable payment method")
            blockers.extend(flat)
        else:
            # Primary path works; broken fallbacks are only a warning.
            warnings.extend(flat)

    # Checkout form + wizard answers — all sourced from the record only.
    if not (ops.get("warehouse_address") or "").strip():
        blockers.append("warehouse address missing (shipping + checkout answer)")
    if not (profile.get("phone") or "").strip():
        blockers.append("contact phone missing (checkout billing telephone)")
    if not ops.get("max_budget"):
        blockers.append("max budget missing (no price cap for autobuy)")
    if not (ops.get("truck_selection") or "").strip():
        blockers.append("truck selection missing (no auto-buy targets — customer can never match a truck)")
    if not ops.get("people_served"):
        blockers.append("people served missing (checkout question)")
    if not (ops.get("distribution_method") or "").strip():
        blockers.append("distribution method missing (checkout question)")

    # Softer gaps.
    if not (profile.get("full_name") or "").strip():
        warnings.append("contact full name missing")
    if not (profile.get("organization_name") or "").strip():
        warnings.append("organization name missing")
    if not (profile.get("email") or "").strip():
        warnings.append("contact email missing (customer gets no purchase alerts)")
    if ops.get("has_loading_dock") is None:
        warnings.append("loading dock question unanswered (checkout will answer: no dock)")

    return {"ok": not blockers, "blockers": blockers, "warnings": warnings}


# ============================================================
# Persistence + alerting
# ============================================================

def cards_meta(rec: dict) -> list[dict]:
    """Non-sensitive card summary for the local mirror: last4 + network +
    normalized expiry + usability. Safe to store (no PAN, no CVV)."""
    profile = rec.get("profile") or {}
    out = []
    for i, pm in enumerate(rec.get("payment_methods") or [], 1):
        out.append({
            "rank": pm.get("rank"),
            "network": pm.get("card_network"),
            "last4": (pm.get("card_number") or "")[-4:],
            "expiry": expiry_mmyy(pm.get("exp_month"), pm.get("exp_year")),
            "usable": not _check_card(pm, i, profile),
        })
    return out


def record_flags(customer_id: str, result: dict, meta: list[dict] | None = None) -> dict | None:
    """Persist flags (+ card summary) onto the customers row. Returns the
    PREVIOUS issues dict (or None) so callers can detect newly-broken records."""
    if not customer_id:
        return None
    with get_conn() as c:
        row = c.execute(
            "SELECT data_ok, data_issues FROM customers WHERE id = ?",
            (customer_id,),
        ).fetchone()
        prev = None
        if row and row["data_issues"]:
            try:
                prev = json.loads(row["data_issues"])
            except (ValueError, TypeError):
                prev = None
        c.execute(
            """UPDATE customers
                  SET data_ok = ?, data_issues = ?, cards_meta = ?,
                      data_checked_at = datetime('now')
                WHERE id = ?""",
            (
                1 if result["ok"] else 0,
                json.dumps({"blockers": result["blockers"], "warnings": result["warnings"]}),
                json.dumps(meta) if meta is not None else None,
                customer_id,
            ),
        )
    return prev


def _alert_operator(rec: dict, result: dict, new_blockers: list[str]) -> None:
    """Telegram the operator about a newly-incomplete customer. Best-effort."""
    profile = rec.get("profile") or {}
    name = profile.get("organization_name") or profile.get("full_name") or rec.get("id") or "?"
    lines = "\n".join(f"  • {html.escape(b)}" for b in result["blockers"])
    msg = (
        f"⚠️ <b>Customer data incomplete — auto-buy would fail</b>\n"
        f"Customer: <b>{html.escape(str(name))}</b> ({html.escape(str(rec.get('id') or ''))})\n"
        f"Blockers:\n{lines}\n"
        f"Fix the record in QuickBeed — the flag clears automatically on the next check."
    )
    delivered, err = False, None
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_OPERATOR_CHAT_ID") or "").strip()
    if token and chat:
        try:
            resp = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat, "text": msg, "parse_mode": "HTML",
                      "disable_web_page_preview": True},
                timeout=10,
            )
            delivered = bool(resp.json().get("ok"))
            if not delivered:
                err = resp.text[:200]
        except Exception as exc:  # noqa: BLE001
            err = f"{type(exc).__name__}: {exc}"
    else:
        err = "TELEGRAM_BOT_TOKEN / TELEGRAM_OPERATOR_CHAT_ID not configured"
    print(f"[readiness] customer {rec.get('id')} flagged: {result['blockers']} "
          f"(telegram delivered={delivered}{' err=' + err if err else ''})")
    try:
        import notifications_log
        notifications_log.record_telegram(
            source="customer_readiness", message=msg, delivered=delivered,
            error=err, channel="operator", level="warning",
            title=f"Customer data incomplete: {name}",
        )
    except Exception:  # noqa: BLE001
        pass


def validate_and_flag(rec: dict, *, alert: bool = True) -> dict:
    """Validate a full record, persist flags + card summary, alert on NEW blockers."""
    result = validate_record(rec)
    prev = record_flags(rec.get("id"), result, cards_meta(rec))
    if alert and result["blockers"]:
        prev_blockers = set((prev or {}).get("blockers") or [])
        new = [b for b in result["blockers"] if b not in prev_blockers]
        if new:
            _alert_operator(rec, result, new)
    return result


# ============================================================
# Sweep + poll-loop hook
# ============================================================

def sweep_all(*, statuses=SWEEP_STATUSES) -> dict:
    """Fetch + validate every customer in `statuses`. Each fetch_full()
    already validates and flags via its hook; we re-run the pure
    validation here only to build the summary."""
    import quickbeed  # deferred: quickbeed imports us lazily too

    placeholders = ",".join("?" * len(statuses))
    with get_conn() as c:
        rows = c.execute(
            f"SELECT id, organization_name FROM customers WHERE status IN ({placeholders})",
            list(statuses),
        ).fetchall()

    checked, flagged, errors = 0, [], []
    for r in rows:
        try:
            rec = quickbeed.fetch_full(r["id"], reason=quickbeed.REASON_RECONCILIATION)
            checked += 1
            res = validate_record(rec)
            if not res["ok"]:
                flagged.append({"id": r["id"], "name": r["organization_name"],
                                "blockers": res["blockers"]})
        except Exception as exc:  # noqa: BLE001
            errors.append({"id": r["id"], "error": f"{type(exc).__name__}: {exc}"})
    quickbeed.set_sync_state(SWEEP_STATE_KEY, _utcnow_iso())
    summary = {"checked": checked, "flagged": flagged, "errors": errors,
               "swept_at": _utcnow_iso()}
    print(f"[readiness] sweep: {checked} checked, {len(flagged)} flagged, "
          f"{len(errors)} errors")
    return summary


def poll_tick(changed_ids: list[str] | None = None) -> None:
    """Called from the QuickBeed poll loop after each incremental sync.

    Re-validates any customer whose record just changed (the list endpoint
    carries no cards, so a full fetch is needed) and runs the daily sweep
    when due. Never raises — the poll loop must survive us."""
    import quickbeed

    for cid in changed_ids or []:
        try:
            quickbeed.fetch_full(cid, reason=quickbeed.REASON_RECONCILIATION)
        except Exception:  # noqa: BLE001
            traceback.print_exc()

    try:
        last = quickbeed.get_sync_state(SWEEP_STATE_KEY)
        due = True
        if last:
            try:
                last_dt = datetime.fromisoformat(last.replace("Z", "+00:00"))
                due = (datetime.now(timezone.utc) - last_dt).total_seconds() >= SWEEP_INTERVAL_S
            except ValueError:
                due = True
        if due:
            sweep_all()
    except Exception:  # noqa: BLE001
        traceback.print_exc()


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
