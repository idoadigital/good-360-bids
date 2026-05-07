"""Pass-2 test-buy runner: drives Good360 checkout through the
DevTools MCP agent (good360_devtools_agent), using:

  - master scan account creds for login (SCAN_GOOD360_*)
  - form-supplied buyer name + email for checkout buyer fields
  - form-supplied test card for payment

Card data is held in memory only — never persisted, never logged.
The agent is invoked with `dry_run=True` by default so it stops before
clicking Place Order. Live-submit mode requires both
DEVTOOLS_AGENT_ALLOW_LIVE_PURCHASE and DEVTOOLS_AGENT_ALLOW_SECRETS_TO_MODEL
to be set in Settings — those checks live in the agent's
`_validate_purchase_context` and we surface their errors verbatim.
"""
from __future__ import annotations

import asyncio
import os
import sys
import threading
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Path setup so we can import settings_bootstrap and good360_devtools_agent
# from any container (the missioncontrol container has /app on disk via
# the image build; the monitor's bind mount also includes /app).
_REPO_ROOT_CANDIDATES = ["/app", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]
for _p in _REPO_ROOT_CANDIDATES:
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Hydrate creds from the encrypted dashboard DB.
try:
    import settings_bootstrap  # noqa: F401
except Exception:
    pass

UTC = timezone.utc
WORKDIR = os.environ.get("WORKDIR", "/app/workdir")

_RUNNER_LOCK = threading.Lock()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _update_test_run(test_id: int, **fields) -> None:
    """Best-effort UPDATE on the test_runs row. Imports lazily so this
    module is safe to import even if dashboard modules aren't on the path."""
    try:
        from db import get_conn  # type: ignore
    except ImportError:
        if "/app/missioncontrol" not in sys.path:
            sys.path.insert(0, "/app/missioncontrol")
        from db import get_conn  # type: ignore
    if not fields:
        return
    cols = ", ".join(f"{k} = ?" for k in fields)
    vals = list(fields.values()) + [test_id]
    try:
        with get_conn() as c:
            c.execute(f"UPDATE test_runs SET {cols} WHERE id = ?", vals)
    except Exception:
        pass


def run_in_background(
    *,
    test_id: int,
    customer_name: str,
    customer_email: str,
    truck_url: Optional[str],
    card_number: str,
    card_expiry: str,
    card_cvv: str,
    dry_run: bool = True,
) -> None:
    """Fire-and-forget runner. Caller wraps in threading.Thread."""
    started_iso = _now_iso()
    _update_test_run(test_id, status="queued",
                     result_summary="waiting for runner lock — another test in flight")

    with _RUNNER_LOCK:
        _update_test_run(test_id, status="running", started_at=started_iso,
                         result_summary="bootstrapping MCP agent…", error=None)

        try:
            outcome = _execute_via_agent(
                test_id=test_id,
                customer_name=customer_name,
                customer_email=customer_email,
                truck_url=truck_url,
                card_number=card_number,
                card_expiry=card_expiry,
                card_cvv=card_cvv,
                dry_run=dry_run,
            )
        except Exception as exc:
            outcome = {
                "status": "failed",
                "summary": f"runner crashed: {type(exc).__name__}: {exc}",
                "error": traceback.format_exc(),
            }
        finally:
            # Belt-and-braces: scrub local refs to PAN/CVV.
            card_number = "*" * len(card_number) if card_number else ""
            card_cvv = "*" * len(card_cvv) if card_cvv else ""

    _update_test_run(
        test_id,
        status=outcome.get("status", "completed"),
        finished_at=_now_iso(),
        result_summary=(outcome.get("summary") or "")[:500],
        error=(outcome.get("error") or None) if outcome.get("error") else None,
    )


def _execute_via_agent(
    *,
    test_id: int,
    customer_name: str,
    customer_email: str,
    truck_url: Optional[str],
    card_number: str,
    card_expiry: str,
    card_cvv: str,
    dry_run: bool,
) -> dict:
    """Invoke good360_devtools_agent.run_agent with form data. Returns:
       {status, summary, error}. The agent's result schema is
       SUCCESS|MISSED|FAILED|MANUAL|DRY_RUN|BLOCKED — we map those onto
       our own test_runs.status (completed | failed) for UI consistency."""
    if not truck_url:
        return {
            "status": "failed",
            "summary": "Truck URL required (auto-pick not yet supported)",
            "error": "Provide a truck URL in the form to run a real test.",
        }

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        return {
            "status": "failed",
            "summary": "OPENAI_API_KEY not configured",
            "error": (
                "The MCP agent needs an OpenAI API key. Add OPENAI_API_KEY in "
                "dashboard Settings (it's already in the secrets manifest), then "
                "restart the missioncontrol container so settings_bootstrap "
                "picks it up."
            ),
        }
    if not (os.environ.get("SCAN_GOOD360_EMAIL", "").strip()
            and os.environ.get("SCAN_GOOD360_PASSWORD", "").strip()):
        return {
            "status": "failed",
            "summary": "Master scan credentials not configured",
            "error": (
                "SCAN_GOOD360_EMAIL / SCAN_GOOD360_PASSWORD are empty. Fill them "
                "in Settings; the test runner uses the master account to log in."
            ),
        }

    _update_test_run(test_id, result_summary="building agent prompt…")

    # Synthetic "org" override carrying the master login + the form's test card
    # + the form's buyer info. The agent uses these the same way it'd use a
    # real org's stored data.
    expiry_clean = "".join(c for c in (card_expiry or "") if c.isdigit())
    org_override = {
        "name": customer_name or "Mission Control test buyer",
        "good360_email":    os.environ["SCAN_GOOD360_EMAIL"],
        "good360_password": os.environ["SCAN_GOOD360_PASSWORD"],
        "card": {
            "name":    customer_name or "",
            "number":  card_number,
            "expiry":  expiry_clean,
            "cvv":     card_cvv,
            "type":    "visa",      # 4242…4242 prefix
        },
        "warehouse_address":  "",
        "billing_address":    "",
        "checkout_answers":   {
            # Sensible placeholders so checkout questions fill on the way to payment.
            "people_helped":          "150",
            "distribution_method":    "Distributed through community outreach",
            "warehouse_address":      "1025 Progress Circle Lawrenceville, GA 30043",
            "dock_pallet":            "Yes, we have a dock and pallet jack",
        },
        "buyer_name":  customer_name,
        "buyer_email": customer_email,
        "max_auto_pay": float(os.environ.get("MAX_AUTO_PAY", "6400")),
    }

    try:
        import good360_devtools_agent as _agent  # type: ignore
    except Exception as e:
        return {
            "status": "failed",
            "summary": "could not import devtools agent",
            "error": f"{type(e).__name__}: {e}",
        }

    _update_test_run(test_id,
                     result_summary=("dry-run mode — agent will stop before Place Order"
                                     if dry_run else
                                     "live mode — agent will click Place Order"))

    # The agent is async (Runner.run is awaited). Each thread gets its own
    # event loop. Long-running: 1–5 minutes for a full checkout.
    try:
        result = asyncio.run(_agent.run_agent(
            org_key="testbuy",          # purely informational; org_override bypasses _load_org
            truck_name="Mission Control test buy",
            truck_url=truck_url,
            admin_fee=0.0,
            dry_run=dry_run,
            org_override=org_override,
        ))
    except RuntimeError as e:
        # Likely from _validate_purchase_context: missing OPENAI_API_KEY,
        # missing safety flag, etc. Surface the message verbatim.
        return {"status": "failed", "summary": str(e), "error": str(e)}
    except Exception as e:
        return {
            "status": "failed",
            "summary": f"agent crashed: {type(e).__name__}",
            "error": traceback.format_exc(),
        }

    # Normalize the CheckoutAgentResult into our test_runs row shape.
    agent_status = (getattr(result, "status", None) or "FAILED").upper()
    agent_msg    = getattr(result, "message", "") or ""
    evidence     = getattr(result, "evidence", []) or []
    final_url    = getattr(result, "final_url", "") or ""
    confirmation = getattr(result, "confirmation_number", "") or ""
    order_total  = getattr(result, "order_total", None)

    summary_map = {
        "SUCCESS":  f"ORDER CONFIRMED — confirmation {confirmation or '(missing)'} · total ${order_total or '?'}",
        "DRY_RUN":  "DRY RUN — agent stopped before clicking Place Order (dry_run mode)",
        "FAILED":   "FAILED — payment declined or checkout error",
        "MISSED":   "MISSED — truck became unavailable during checkout",
        "MANUAL":   "MANUAL — agent flagged the order as needing human review",
        "BLOCKED":  "BLOCKED — agent refused to proceed (safety / unexpected page)",
    }
    summary = summary_map.get(agent_status, f"{agent_status}: {agent_msg}")
    error_blob = "\n\n".join([
        f"agent message: {agent_msg}" if agent_msg else "",
        f"final URL: {final_url}" if final_url else "",
        ("evidence:\n  - " + "\n  - ".join(evidence)) if evidence else "",
    ]).strip() or None

    # Map agent statuses onto our test_runs.status:
    # SUCCESS / DRY_RUN  → completed (the test ran end-to-end as designed)
    # FAILED / MISSED / MANUAL / BLOCKED → also completed (we captured a real
    #   answer from Good360); failure is the *checkout* outcome, not the
    #   *test* outcome.
    return {"status": "completed", "summary": summary, "error": error_blob}
