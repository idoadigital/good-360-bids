"""Readiness-check CLI: run the production autobuy code path against every
active in-rotation customer with a fake (decline-only) card, and print a
pass/fail-per-stage matrix.

This calls the same `autobuy_v2.attempt_purchase(..., test_card_override=...)`
that the admin dashboard's /api/admin/autobuy-readiness-check endpoint calls,
so a passing run here is empirical proof that real autobuy works for that
customer (modulo the expected card-decline at the end).

Usage (inside the monitor or missioncontrol container):
    python3 /tmp/_readiness_check_all.py \\
        --truck-url https://catalog.good360.org/marketplace/<truck>.html \\
        [--truck-name "Amazon ... Truckload - Maysville, KY"]

Side effects are suppressed inside autobuy_v2 when test_card_override is
supplied: no cooldown, no finding-fee billing, no operator notification.
"""
import argparse
import os
import sys
import time

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/good360_roster")
sys.path.insert(0, "/root/good-360-bids")
sys.path.insert(0, "/root/good-360-bids/good360_roster")

from good360_autobuy_v2 import attempt_purchase  # noqa: E402
from roster_orchestrator import get_db_connection, log_truck_event  # noqa: E402

_FAKE_CARD = {
    "card_holder_name": "Readiness Test",
    "card_number": "4000000000000002",
    "card_last4": "0002",
    "card_expiry_month": 12,
    "card_expiry_year": 2030,
    "card_cvv": "999",
    "billing_zip": "30046",
    "card_type": "visa",
}

_STAGES = [
    ("credentials_missing", "no creds in QuickBeed"),
    ("login",               "Good360 login"),
    ("add_to_cart",         "add-to-cart / eligibility"),
    ("checkout_questions",  "checkout questions"),
    ("shipping_address",    "shipping address"),
    ("payment_form",        "payment form render"),
    ("place_order_focus",   "Place Order click"),
    ("card_declined",       "card declined ✅ (test passes)"),
    ("order_confirmed",     "order placed (unexpected with fake card)"),
    ("unknown",             "unclassified"),
]


def _classify(success: bool, status: str, msg: str) -> str:
    msg = (msg or "").lower()
    if success:
        return "order_confirmed"
    if "declined" in msg or "card was rejected" in msg or "payment failed" in msg \
            or "payment was declined" in msg:
        return "card_declined"
    # credential / login checks BEFORE downstream-stage keywords. Some agent
    # responses describe the login failure by listing the steps it blocks
    # ("blocks Add to Cart, Checkout, payment") — without this priority we'd
    # mis-classify those as add_to_cart.
    if "no usable payment_methods" in msg or "no partner credentials" in msg \
            or "no good360 credentials" in msg or "credentials are rejected" in msg \
            or "could not sign in" in msg:
        return "credentials_missing"
    if ("login did not complete" in msg or "login failed" in msg or "sign-in" in msg
            or "sign in" in msg or "log in" in msg
            or "not authorized to log in" in msg or "valid email address" in msg
            or "account sign-in was incorrect" in msg):
        return "login"
    if "place order" in msg or "payment method step" in msg or "credit card details" in msg:
        return "place_order_focus"
    if "card field" in msg or "payment field" in msg or "payment step did not load" in msg \
            or "billing or card fields" in msg:
        return "payment_form"
    if "shipping address" in msg or "warehouse address" in msg:
        return "shipping_address"
    if "checkout question" in msg or "answer questions" in msg:
        return "checkout_questions"
    if ("out of stock" in msg or "checkout is disabled" in msg or "quote" in msg
            or "could not add" in msg or "add to cart" in msg or "could not locate" in msg
            or "full truckload" in msg or "truckload/general-products" in msg
            or "existing cart" in msg or "cart cleanup" in msg
            or "cannot be added" in msg or "disabled" in msg):
        return "add_to_cart"
    return "unknown"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truck-url", required=True)
    ap.add_argument("--truck-name", default="(readiness check)")
    ap.add_argument("--org-ids", default="",
                    help="comma-separated nonprofits.id list; default = all in-rotation")
    args = ap.parse_args()

    with get_db_connection() as c:
        if args.org_ids:
            ids = [int(x) for x in args.org_ids.split(",") if x.strip()]
            placeholder = ",".join("?" * len(ids))
            rows = c.execute(
                f"SELECT id, org_name, quickbeed_customer_id FROM nonprofits "
                f"WHERE id IN ({placeholder}) ORDER BY id", ids).fetchall()
        else:
            rows = c.execute(
                "SELECT id, org_name, quickbeed_customer_id FROM nonprofits "
                "WHERE auto_buy_global=1 ORDER BY id").fetchall()

    if not rows:
        print("no customers to check; exiting"); return

    print(f"running readiness check against {len(rows)} customer(s)")
    print(f"  truck_url:  {args.truck_url}")
    print(f"  truck_name: {args.truck_name}")
    print(f"  fake card:  visa ****0002 (universal card_declined PAN)")
    print()

    results = []
    for r in rows:
        org_id = r[0]; org_name = r[1]
        print(f"[{time.strftime('%H:%M:%S')}] org_id={org_id}  {org_name!r}  starting…")
        started = time.time()
        try:
            event_id = log_truck_event(
                truck_title=args.truck_name,
                truck_url=args.truck_url,
                truck_price=0.0, truck_location="", truck_category="other",
                raw_data={"_readiness_check": True},
            )
            result = attempt_purchase(org_id, event_id,
                                     test_card_override=_FAKE_CARD)
            stage = _classify(result.success, result.status, result.error_message or "")
            elapsed = int(time.time() - started)
            results.append({
                "org_id": org_id, "org_name": org_name,
                "stage": stage, "status": result.status,
                "elapsed_s": elapsed,
                "error": (result.error_message or "")[:300],
            })
            print(f"[{time.strftime('%H:%M:%S')}] org_id={org_id}  stage={stage}  status={result.status}  ({elapsed}s)")
        except Exception as e:
            elapsed = int(time.time() - started)
            results.append({
                "org_id": org_id, "org_name": org_name,
                "stage": "unknown", "status": "crash",
                "elapsed_s": elapsed, "error": f"{type(e).__name__}: {e}",
            })
            print(f"[{time.strftime('%H:%M:%S')}] org_id={org_id}  CRASHED: {e}")

    print()
    print("=" * 78)
    print("READINESS MATRIX")
    print("=" * 78)
    print(f"{'org_id':<7} {'stage':<22} {'status':<20} {'elapsed':<8} customer")
    print("-" * 78)
    for r in results:
        print(f"{r['org_id']:<7} {r['stage']:<22} {r['status']:<20} {str(r['elapsed_s'])+'s':<8} {r['org_name']}")
    print()
    print("legend: stage tells you where the run ended")
    for code, desc in _STAGES:
        marker = "✅" if code == "card_declined" else (
                 "❓" if code in ("unknown", "order_confirmed") else "❌")
        print(f"  {marker} {code:<22} — {desc}")


if __name__ == "__main__":
    main()
