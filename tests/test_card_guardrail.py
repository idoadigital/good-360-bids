"""Failing-first: STRICT CARD GUARDRAIL (operator directive 2026-06-12).

If a payment fails because of the card: record the failure, alert the
operator with the processor's error message, and STOP. Under no
circumstances may the engine use a card not attached to the customer's
account — the master-card fallback call must not exist in the purchase
path, regardless of any flag.

1. attempt_purchase contains NO call to handle_master_card_fallback.
2. The guardrail is explicit in the code (a marked refusal block), so a
   future editor sees it's deliberate, not an accident.
3. _alert_payment_failure exists and the final failure path calls it
   (operator gets the processor error via Telegram).
"""
import inspect
import sys

sys.path.insert(0, "/app/good360_roster")
sys.path.insert(0, "/app/missioncontrol")
import good360_autobuy_v2 as ab

failures = []
src = inspect.getsource(ab.attempt_purchase)

if "handle_master_card_fallback(" in src:
    failures.append("attempt_purchase still CALLS handle_master_card_fallback — "
                    "a non-customer card could be used")
if "STRICT CARD GUARDRAIL" not in src:
    failures.append("guardrail refusal block not marked in attempt_purchase")
if not hasattr(ab, "_alert_payment_failure"):
    failures.append("_alert_payment_failure does not exist")
elif "_alert_payment_failure" not in src:
    failures.append("final failure path does not alert the operator")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: card guardrail enforced — no fallback call, failure alerts wired")
