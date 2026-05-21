"""One-off live-autobuy test harness — clean version.

Now uses the public `test_card_override` parameter on
`autobuy_v2.attempt_purchase`, so this script is just a thin wrapper:

  - log a truck_event for the URL
  - call attempt_purchase(org_id, truck_event_id, test_card_override=FAKE)

All the heavy lifting (loader tolerance, fake-card injection, side-effect
suppression, dry-run gate bypass) now lives in autobuy_v2 itself.

Env:
  TEST_TARGET_ORG_ID — roster.db nonprofits.id (default 1 = Hope 4 Humanity)
  TEST_TRUCK_URL     — defaults to the live softlines URL
  TEST_TRUCK_NAME    — defaults to the matching truck title
"""
import os
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/good360_roster")

from good360_autobuy_v2 import attempt_purchase  # noqa: E402
import roster_orchestrator  # noqa: E402


FAKE_CARD = {
    "card_holder_name": "Test Buyer",
    "card_number": "4000000000000002",   # universal "card_declined" test PAN
    "card_last4": "0002",
    "card_expiry_month": 12,
    "card_expiry_year": 2030,
    "card_cvv": "999",
    "billing_zip": "30046",
    "card_type": "visa",
}


def main():
    target_org_id = int(os.environ.get("TEST_TARGET_ORG_ID", "1"))
    truck_url = os.environ.get("TEST_TRUCK_URL",
                              "https://catalog.good360.org/marketplace/mv-softlines-tl-placeholder.html")
    truck_name = os.environ.get("TEST_TRUCK_NAME",
                                "Amazon Assorted Softlines Truckload - Maysville, KY")
    event_id = roster_orchestrator.log_truck_event(
        truck_title=truck_name,
        truck_url=truck_url,
        truck_price=0.0,
        truck_location="Maysville, KY",
        truck_category="other",
        raw_data={"_test_harness": True, "_fake_card_pan_last4": "0002"},
    )
    print(f"[harness] event_id={event_id}  org_id={target_org_id}  url={truck_url}")
    result = attempt_purchase(target_org_id, event_id, test_card_override=FAKE_CARD)
    print(f"[harness] result: success={result.success}  status={result.status}")
    print(f"[harness] conf={result.confirmation_number}  total={result.order_total}")
    if result.error_message:
        print(f"[harness] error: {result.error_message}")


if __name__ == "__main__":
    main()
