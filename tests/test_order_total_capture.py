"""Order-total capture (fix for "Approved $0.00" purchases, 2026-07-08).

Every daemon-path purchase since 2026-06-12 recorded order_total NULL
because the daemon never scraped the checkout total and the caller fell
back to truck_price, which Good360 hides from the scan account (0.0).

1. _parse_order_total prefers a labelled Total line over larger
   retail/MSRP amounts, falls back to the largest $-amount, and never
   raises.
2. BrowserManager keeps totals per org_key (no cross-org bleed).
3. Both /checkout and /test_checkout responses carry order_total.
4. _run_checkout_via_daemon prefers the daemon payload total; truck_price
   is only the fallback.
"""
import inspect
import sys

sys.path.insert(0, "/app")
sys.path.insert(0, "/app/good360_roster")
sys.path.insert(0, "/app/missioncontrol")

import good360_daemon as gd
import good360_autobuy_v2 as ab

failures = []

# --- 1. parser behaviour -------------------------------------------------
review_page = """
Amazon Assorted Houseware Truckload - Maysville, KY
Total Retail Value: $58,320.00
Estimated MSRP $61,000.00
Subtotal $6,400.00
Shipping $0.00
Order Total $6,400.00
Place Order
"""
got = gd._parse_order_total(review_page)
if got != 6400.00:
    failures.append(f"labelled total not preferred: got {got!r}, want 6400.0")

no_label = "Admin fee $5,837.12 applies. Support line $12.00."
got = gd._parse_order_total(no_label)
if got != 5837.12:
    failures.append(f"fallback max failed: got {got!r}, want 5837.12")

if gd._parse_order_total("no amounts here") is not None:
    failures.append("expected None when page has no $-amounts")
if gd._parse_order_total("") is not None:
    failures.append("expected None on empty text")
if gd._parse_order_total("Total $1,234.56") != 1234.56:
    failures.append("comma-grouped amount parse failed")

# --- 2. per-org storage ---------------------------------------------------
mgr = gd.BrowserManager()
if not isinstance(getattr(mgr, "last_order_totals", None), dict):
    failures.append("BrowserManager.last_order_totals dict missing")
else:
    mgr.last_order_totals["org_a"] = 100.0
    if mgr.last_order_totals.get("org_b") is not None:
        failures.append("totals bleed across org keys")

inner_src = inspect.getsource(gd.BrowserManager._checkout_inner)
if "last_order_totals.pop(org_key" not in inner_src:
    failures.append("_checkout_inner does not clear the org's stale total")
if "_parse_order_total" not in inner_src:
    failures.append("_checkout_inner never parses the order total")

# --- 3. HTTP responses carry the total ------------------------------------
post_src = inspect.getsource(gd.Handler._dispatch_post)
if post_src.count("'order_total':") < 2:
    failures.append("/checkout and /test_checkout responses must both "
                    "include order_total")

# --- 4. caller prefers the payload total -----------------------------------
via_daemon_src = inspect.getsource(ab._run_checkout_via_daemon)
if 'payload.get("order_total")' not in via_daemon_src:
    failures.append("_run_checkout_via_daemon ignores the daemon's "
                    "order_total payload field")

if failures:
    print("FAIL:")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("PASS: order-total capture wired review-page → daemon payload → "
      "CheckoutResult, truck_price demoted to fallback")
