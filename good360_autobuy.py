#!/usr/bin/env python3
"""Good360 Auto-Buy Script - FIXED VERSION v4

Fixes:
1. Proper 3-state success detection: SUCCESS / FAILED / MISSED
2. Speed optimization - reduced wait times
3. Real order confirmation detection
4. Truck missed detection
"""

import json
import os
import re
import subprocess
import sys
import time
import traceback
from datetime import datetime

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright
except ImportError:
    print("[AUTO-BUY] Installing playwright...")
    subprocess.run([sys.executable, '-m', 'pip', 'install', 'playwright'], capture_output=True)
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

# audit_log is the immutable append-only system-of-record for money-moving
# events. Every purchase outcome below writes one entry so postmortems
# don't depend on parsing stdout or trusting in-process state.
from audit_log import audit as audit_event

# Cross-process exclusive lock. Both the monitor (this script's caller)
# and the daemon's persistent-browser path can race on the same truck;
# fcntl.flock on a per-truck file under the shared workdir volume
# serialises them so we never double-charge.
from purchase_lock import exclusive_purchase_lock

# Environment master switch — staging/feature stacks set
# ENABLE_AUTO_BUY=false and this script must refuse to run there.
import feature_flags

# Load config. The JSON file is .gitignored — fresh deployments don't have it,
# and modern deployments source creds from env (.env / dashboard settings store)
# rather than this file. Treat the file as optional so a missing/empty config
# doesn't break import.
try:
    with open('good360_checkout_config.json') as f:
        config = json.load(f)
except (FileNotFoundError, json.JSONDecodeError):
    config = {}

# Sandbox-mode router (URL + creds + card swap when SANDBOX_MODE=true).
# Sibling repo file; injected onto sys.path so the script keeps working when
# invoked as a subprocess from monitor.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sandbox  # noqa: E402

# ============================================================
# Debug capture
# ============================================================
# Live-mode failures are hard to triage from a screenshot + 1500 chars of
# tail output. The capture collects everything Playwright sees during the
# run — every HTTP response (Good360 API errors, 4xx/5xx, redirects), every
# browser console message (JS errors that block submit), the final URL,
# and a snippet of the final page HTML. Written to disk at process exit
# whether the run succeeded or failed, so the dashboard can show what
# happened.
import atexit  # noqa: E402

_CAPTURE: dict = {}
_CAPTURE_RESPONSE_CAP = 16 * 1024   # per-response body cap
_CAPTURE_RESPONSE_LIMIT = 200       # keep newest N responses
_CAPTURE_CONSOLE_LIMIT = 200


def _capture_reset(truck_name: str, truck_url: str, screenshot_dir: str):
    workdir = os.environ.get("WORKDIR", "/app/workdir")
    out_dir = os.path.join(workdir, "checkout_captures")
    os.makedirs(out_dir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", truck_name)[:60]
    _CAPTURE.clear()
    _CAPTURE.update({
        "started_at":   datetime.now().isoformat(),
        "truck_name":   truck_name,
        "truck_url":    truck_url,
        "screenshot_dir": screenshot_dir,
        "sandbox_mode": (os.environ.get("SANDBOX_MODE", "") or "").lower() in ("1","true","yes","on"),
        "network":      [],
        "console":      [],
        "steps":        [],
        "final_url":    None,
        "final_html":   None,
        "outcome":      None,
        "message":      None,
        "written":      False,
        "path":         os.path.join(out_dir, f"{ts}_{safe}.json"),
    })


def _capture_attach(page):
    """Subscribe to network + console events. Best-effort: any listener
    error is silenced so a capture problem can't crash the actual checkout.
    """
    def on_response(resp):
        try:
            body = ""
            ct = (resp.headers.get("content-type") or "").lower()
            # Read bodies only for JSON / text / form content — skip binary.
            if any(k in ct for k in ("json", "text", "html", "xml", "javascript", "form")):
                try:
                    raw = resp.body()
                    body = raw[:_CAPTURE_RESPONSE_CAP].decode("utf-8", errors="replace")
                    if len(raw) > _CAPTURE_RESPONSE_CAP:
                        body += f"\n…[truncated {len(raw)-_CAPTURE_RESPONSE_CAP} bytes]"
                except Exception:
                    body = ""
            _CAPTURE["network"].append({
                "ts":           datetime.now().isoformat(timespec="milliseconds"),
                "method":       resp.request.method,
                "url":          resp.url,
                "status":       resp.status,
                "status_text":  resp.status_text,
                "content_type": resp.headers.get("content-type") or "",
                "size":         len(body),
                "body":         body,
            })
            # Trim from the front so we keep newest entries.
            if len(_CAPTURE["network"]) > _CAPTURE_RESPONSE_LIMIT:
                del _CAPTURE["network"][: len(_CAPTURE["network"]) - _CAPTURE_RESPONSE_LIMIT]
        except Exception:
            pass

    def on_console(msg):
        try:
            _CAPTURE["console"].append({
                "ts":   datetime.now().isoformat(timespec="milliseconds"),
                "type": msg.type,
                "text": msg.text[:2000],
            })
            if len(_CAPTURE["console"]) > _CAPTURE_CONSOLE_LIMIT:
                del _CAPTURE["console"][: len(_CAPTURE["console"]) - _CAPTURE_CONSOLE_LIMIT]
        except Exception:
            pass

    def on_pageerror(err):
        try:
            _CAPTURE["console"].append({
                "ts":   datetime.now().isoformat(timespec="milliseconds"),
                "type": "pageerror",
                "text": str(err)[:2000],
            })
        except Exception:
            pass

    page.on("response", on_response)
    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    # Stash a reference so the finally-block can read the live URL even if
    # `page` has gone out of scope.
    _CAPTURE["_page_ref"] = page


def _capture_step(label: str, extra: dict | None = None):
    _CAPTURE.setdefault("steps", []).append({
        "ts":    datetime.now().isoformat(timespec="milliseconds"),
        "label": label,
        **(extra or {}),
    })


def _capture_finalize(outcome: str | None = None, message: str | None = None):
    if _CAPTURE.get("written"):
        return _CAPTURE.get("path")
    _CAPTURE["written"] = True
    _CAPTURE["finished_at"] = datetime.now().isoformat()
    if outcome is not None:
        _CAPTURE["outcome"] = outcome
    if message is not None:
        _CAPTURE["message"] = message
    page = _CAPTURE.pop("_page_ref", None)
    if page is not None:
        try:
            _CAPTURE["final_url"]  = page.url
            _CAPTURE["final_html"] = page.content()[:64 * 1024]
        except Exception:
            pass
    try:
        with open(_CAPTURE["path"], "w") as f:
            json.dump(_CAPTURE, f, indent=2, default=str)
        print(f"[CAPTURE] {_CAPTURE['path']}")
        return _CAPTURE["path"]
    except Exception as e:
        print(f"[CAPTURE] write failed: {e}")
        return None


# Ensure the capture lands even if the script bails via sys.exit() or
# an unhandled exception.
atexit.register(lambda: _capture_finalize())


# Constants — live values; sandbox.* getters swap them at call time.
# Resolution order: MAX_AUTO_PAY env var → checkout_config JSON → 6400 default.
# The env override lets operators bump the cap for one-off test runs without
# editing the checkout JSON (which is also consumed by other code paths).
MAX_AUTO_PAY = float(
    os.environ.get("MAX_AUTO_PAY")
    or config.get('max_auto_pay')
    or config.get('max_auto_pay_amount')
    or 6400
)
_CUSTOMER_ORG_CONFIG: dict | None = None  # cached for this process

def _fetch_org_config_from_dashboard(customer_id: str) -> dict | None:
    """Fetch the full org_config (creds + billing_address + card +
    checkout_answers + contact info) for a QuickBeed customer.

    Returns the dict on success or None on any failure. Per
    [[feedback-customer-data-only]] this is the source of truth for
    every form-fill value — billing-address, name-on-card, checkout
    answers all come from here, never hardcoded.
    """
    global _CUSTOMER_ORG_CONFIG
    if _CUSTOMER_ORG_CONFIG is not None:
        return _CUSTOMER_ORG_CONFIG
    base = os.environ.get("MISSIONCONTROL_URL", "http://missioncontrol:5001").rstrip("/")
    api_key = os.environ.get("MISSIONCONTROL_API_KEY", "")
    if not api_key:
        print("[AUTO-BUY] MISSIONCONTROL_API_KEY not set — cannot fetch per-customer org_config")
        return None
    url = f"{base}/api/internal/org-config/{customer_id}?reason=credential_use"
    try:
        import urllib.request, urllib.error
        req = urllib.request.Request(url, headers={"X-API-Key": api_key})
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        print(f"[AUTO-BUY] dashboard org_config fetch HTTP {e.code}: {e.reason}")
        return None
    except Exception as e:
        print(f"[AUTO-BUY] dashboard org_config fetch failed: {e}")
        return None
    cfg = (payload or {}).get("org_config") or {}
    if cfg.get("good360_email") and cfg.get("good360_password"):
        _CUSTOMER_ORG_CONFIG = cfg
        return cfg
    print("[AUTO-BUY] per-customer org_config returned empty creds")
    return None


def _fetch_customer_credentials_from_dashboard(customer_id: str):
    """Back-compat shim: returns (email, password). New code should
    call `_fetch_org_config_from_dashboard` directly to also get
    billing_address and card."""
    cfg = _fetch_org_config_from_dashboard(customer_id)
    if not cfg:
        return None, None
    return cfg.get("good360_email"), cfg.get("good360_password")


def _resolve_h4h_credentials():
    """Pick login creds from the most authoritative source available.

    Source order, most specific first:
      1. GOOD360_CUSTOMER_ID env → fetch this customer's partner
         credentials from the dashboard's internal org-config endpoint.
         This is the right source when the monitor's queue manager
         picked a specific QuickBeed customer for the truck.
      2. GOOD360_HOPE4HUMANITY_EMAIL/PASSWORD — per-org overrides set in
         the dashboard's Settings → "Per-org Good360 accounts".
      3. SCAN_GOOD360_EMAIL/PASSWORD — master scan account. In single-org
         installs this IS the H4H login, so it's a safe fallback rather
         than a different identity.
      4. config.json — legacy support; ignored if missing.
    """
    cid = os.environ.get("GOOD360_CUSTOMER_ID", "").strip()
    if cid:
        email, pw = _fetch_customer_credentials_from_dashboard(cid)
        if email and pw:
            print(f"[AUTO-BUY] using partner credentials for customer {cid} ({email})")
            return email, pw
        print(f"[AUTO-BUY] partner credentials lookup failed for {cid} — falling back to env")

    sources = (
        ("GOOD360_HOPE4HUMANITY_*",
         os.environ.get("GOOD360_HOPE4HUMANITY_EMAIL", ""),
         os.environ.get("GOOD360_HOPE4HUMANITY_PASSWORD", "")),
        ("SCAN_GOOD360_*",
         os.environ.get("SCAN_GOOD360_EMAIL", ""),
         os.environ.get("SCAN_GOOD360_PASSWORD", "")),
        ("good360_checkout_config.json",
         config.get('username', ''),
         config.get('password', '')),
    )
    for name, email, pw in sources:
        if email and pw:
            print(f"[AUTO-BUY] using credentials from {name} ({email})")
            return email, pw
    print("[AUTO-BUY] ⚠️  no credentials in any source — login will fail")
    return "", ""


_RAW_USERNAME, _RAW_PASSWORD = _resolve_h4h_credentials()
USERNAME, PASSWORD = sandbox.org_credentials(_RAW_USERNAME, _RAW_PASSWORD)
del _RAW_USERNAME, _RAW_PASSWORD
CHROME_CARD_NAME = config.get('chrome_card_name', 'Kingdom')

# Checkout answers
ANSWERS = {
    'number_of_people': '300',
    'distribution_method': 'We distribute to the homeless and all those in need',
    'warehouse_address': '1025 Progress Circle Lawrenceville, Ga 30043',
    'pallet_jack': 'We have a loading dock and a pallet jack.'
}

# Telegram config (values from .env — see .env.example)
import os as _os

BOT_TOKEN = _os.environ.get('TELEGRAM_BOT_TOKEN', '')
CHAT_ID = _os.environ.get('TELEGRAM_GROUP_HOPE4HUMANITY', '')

def send_telegram(message):
    """Send alert via Telegram"""
    message = sandbox.decorate_alert(message)
    delivered = False
    err = None
    try:
        import requests
        url = f'https://api.telegram.org/bot{BOT_TOKEN}/sendMessage'
        data = {'chat_id': CHAT_ID, 'text': message, 'parse_mode': 'HTML'}
        requests.post(url, json=data, timeout=10)
        delivered = True
        print("[ALERT] Telegram sent")
    except Exception as e:
        err = str(e)
        print(f"[ALERT] Telegram failed: {e}")
    try:
        from notifications_log import record_telegram
        record_telegram(source='autobuy', message=message, delivered=delivered, error=err, channel='org:hope4humanity')
    except Exception:
        pass

def log_step(step_num, step_name, screenshot_dir, page):
    """Log step and take screenshot"""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{screenshot_dir}/{timestamp}_{step_num:02d}_{step_name}.png"
    try:
        page.screenshot(path=filename)
        print(f"[SCREENSHOT] {filename}")
    except:
        pass

def extract_total(page_text):
    """Extract total cost from page"""
    admin_fee = 0
    shipping_fee = 0

    admin_match = re.search(r'Admin fee[^\d]*([\d,]+\.\d{2})', page_text)
    shipping_match = re.search(r'Shipping fee[^\d]*([\d,]+\.\d{2})', page_text)
    total_match = re.search(r'Total[^\d]*([\d,]+\.\d{2})', page_text)

    if admin_match:
        admin_fee = float(admin_match.group(1).replace(',', ''))
    if shipping_match:
        shipping_fee = float(shipping_match.group(1).replace(',', ''))

    total = 0
    if total_match:
        total = float(total_match.group(1).replace(',', ''))
    elif admin_fee > 0:
        total = admin_fee + shipping_fee

    return admin_fee, shipping_fee, total

def check_order_confirmation(page_text, page_url: str = ""):
    """Check if order actually completed.

    Returns (is_confirmed, indicator). The URL signal is the most
    reliable — Good360 redirects to /onepage/success or /order/<id>
    on a real placement. Text indicators are a fallback and must be
    specific enough not to match incidental UI text (`page_text`
    here is the full HTML, which includes CSS class names and
    JS-rendered hidden content).
    """
    # URL signal first — strongest. Real success redirects away
    # from /marketplace/checkout.
    url_lower = (page_url or "").lower()
    for path_token in ("/onepage/success", "/checkout/success", "/order/",
                       "/confirmation", "/thank"):
        if path_token in url_lower:
            return True, f"url:{path_token}"
    page_lower = (page_text or "").lower()
    for indicator in SUCCESS_INDICATORS:
        if indicator in page_lower:
            return True, indicator
    return False, None


def check_card_declined(page_text):
    """True iff the page shows a payment-gateway decline banner.

    A decline still proves the autobuy ran end-to-end and reached
    CyberSource — useful as a strong signal in sandbox tests. Order
    by specificity so the returned indicator is the user-facing
    error wording, not a field label.
    """
    page_lower = (page_text or "").lower()
    decline_markers = (
        'unable to place order',
        'could not process your payment',
        'we could not process your payment',
        'unable to process payment',
        'card was declined', 'declined by',
        'authorization failed', 'authorisation failed',
        'invalid card', 'card number is invalid',
        'cvv check failed',
        'transaction declined', 'transaction failed',
        'payment failed',
        'invalid expir',
    )
    for marker in decline_markers:
        if marker in page_lower:
            return True, marker
    return False, None


# Centralised so the dashboard / settings UI can read & override the list
# without code edits. Adding new copy variants here is the right hook when
# Good360 rephrases their confirmation page.
#
# Indicators must be specific enough that they don't false-match on
# CSS class names, JS string literals, or hidden UI text in the HTML
# returned by `page.content()`. The bare phrase 'payment successful'
# false-fired on the checkout page (some hidden status component had
# it in a class name); we now require longer, more contextual phrases.
SUCCESS_INDICATORS = (
    'thank you for your order',
    'thank you for your purchase',
    'your order has been placed',
    'your order is confirmed',
    'order receipt',
    'checkout complete',
)

# `#` in verbose (?x) regexes starts a comment-to-EOL — escape every literal
# hash with \# so the pattern parses. Plain (case-insensitive) regex without
# verbose mode is clearer for something this short.
_CONFIRMATION_NUMBER_RE = re.compile(
    r"(?:order|confirmation)\s*(?:number|no\.?|#)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{3,30})",
    re.IGNORECASE,
)


def extract_confirmation_number(page_text: str) -> str | None:
    """Pull the order/confirmation number off the page so operators can
    cross-reference the purchase on Good360 without scraping the email."""
    if not page_text:
        return None
    m = _CONFIRMATION_NUMBER_RE.search(page_text)
    return m.group(1) if m else None

def _goto_with_retry(page, url: str, *, wait_until: str = "domcontentloaded",
                     timeout: int = 12000, max_attempts: int = 2):
    """Navigate with a single transient-error retry.

    Good360 occasionally returns a 503 or stalls the initial response for
    a second under load. A single 500ms retry has rescued enough attempts
    in production to be worth the modest worst-case cost (≈1s extra on
    failure). We retry ONLY on PlaywrightTimeout / network-style errors;
    logic failures (truck sold, login rejected) bubble up unretried so
    we don't pile attempts on a doomed flow."""
    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            return page.goto(url, wait_until=wait_until, timeout=timeout)
        except PlaywrightTimeout as e:
            last_err = e
            if attempt < max_attempts:
                print(f"[AUTO-BUY] goto timeout attempt {attempt}/{max_attempts} on {url} — retrying")
                try:
                    time.sleep(0.5)
                except Exception:
                    pass
            else:
                raise
    if last_err:
        raise last_err


def check_truck_missed(page_text):
    """Check if truck sold out during checkout - TRUCK MISSED.

    Indicators must be specific phrases that only appear when the
    item is actually unavailable. The bare phrase 'not available'
    matches benign UI text (e.g. 'store credits not available',
    'discount code not available') and false-fired on healthy
    product pages, so we require more context.
    """
    missed_indicators = [
        'product is not available',
        'item is not available',
        'truck is not available',
        'this product is no longer available',
        'not available at the moment',
        'currently sold out',
        'sold out',
        'out of stock',
        'no longer available',
    ]
    page_lower = page_text.lower()
    for indicator in missed_indicators:
        if indicator in page_lower:
            return True, indicator
    return False, None

# Mirrors good360_daemon.py — Good360's Add-to-Cart is a span-nested
# button with CSS-module class hashes that change every build. Text
# selectors are the most reliable; the `button-green` / `clickable-root`
# class fragments are stable backups.
_ADD_TO_CART_SELECTOR = (
    'button:has-text("Add to cart"), '
    'button:has-text("Add to Cart"), '
    'button:has(span:text-is("Add to cart")), '
    'button:has(span:text-is("Add to Cart")), '
    'button[class*="button-green"]:has-text("Add"), '
    'button[class*="clickable-root"]:has-text("Add to"), '
    '[class*="add-to-cart"], '
    '[data-action="add-to-cart"]'
)


def _ensure_on_product_detail(page, truck_name: str, log_step_fn=None,
                              screenshot_dir=None, step_num=None) -> bool:
    """Some 'truck' URLs are catalog/placeholder pages (e.g.
    /marketplace/mv-softlines-tl-placeholder.html) that list one or more
    actual purchasable trucks as cards. The product detail page with the
    real 'Add to Cart' button only renders after the operator clicks one
    of those cards. This helper detects that case and drills through.

    Returns True when the page has a visible Add-to-Cart by the end,
    False otherwise. Caller treats False as a hard FAILED.
    """
    # Fast path: we're already on a product detail page.
    #
    # Good360's marketplace is a JS-heavy SPA — the initial HTML is
    # mostly shell, the real product cards and the Add-to-Cart button
    # only appear after React hydrates and the catalog data XHR comes
    # back. So we need three waits, not one:
    #   1. wait for domcontentloaded
    #   2. wait for ANY visible interactive element (proves hydration
    #      started)
    #   3. wait_for_selector on the Add-to-Cart button with
    #      state='visible' (this is what actually matters)
    # Using .is_visible() instead of wait_for_selector tries to read
    # the current state once and returns immediately if no element
    # matches yet — which is the bug we kept hitting.
    try:
        page.wait_for_load_state('domcontentloaded', timeout=4000)
    except Exception:
        pass
    try:
        page.wait_for_selector(
            'a:visible, button:visible, [class*="product"]:visible',
            timeout=4000,
        )
    except Exception:
        pass
    try:
        page.wait_for_selector(_ADD_TO_CART_SELECTOR, state='visible', timeout=8000)
        return True
    except Exception:
        pass

    print("[AUTO-BUY] no Add-to-Cart on the URL page — looking for a product card to click through")

    # Look for product-shaped links. Good360's marketplace uses anchor
    # tags into /marketplace/<slug>.html for individual truck listings;
    # filter out the placeholder URL we came from so we don't loop.
    candidates = page.locator(
        'main a[href*="/marketplace/"], '
        'article a[href*="/marketplace/"], '
        'a.product-card[href*="/marketplace/"], '
        '.product-card a[href*="/marketplace/"]'
    )

    n = 0
    try:
        n = min(candidates.count(), 30)
    except Exception:
        n = 0
    if n == 0:
        print("[AUTO-BUY] no product-card links found on the landing page")
        return False

    # Prefer a link whose visible text shares a distinctive token with
    # the truck name. "Amazon Assorted Softlines Truckload" → match on
    # 'softlines', 'truckload', 'amazon' (in that priority order — most
    # specific first). Tokens shorter than 4 chars are skipped (too generic).
    name_lc = (truck_name or "").lower()
    tokens = [w for w in re.split(r'[\s\-_,]+', name_lc) if len(w) >= 4]
    chosen_idx = -1
    if tokens:
        for i in range(n):
            try:
                text = candidates.nth(i).inner_text().strip().lower()
            except Exception:
                continue
            if any(t in text for t in tokens):
                chosen_idx = i
                break

    if chosen_idx == -1:
        chosen_idx = 0  # fall back to the first product-shaped link

    try:
        target_text = candidates.nth(chosen_idx).inner_text().strip()
    except Exception:
        target_text = "(unreadable)"
    print(f"[AUTO-BUY] clicking through to product: {target_text[:80]!r}")

    try:
        candidates.nth(chosen_idx).click()
    except Exception as e:
        print(f"[AUTO-BUY] product-link click failed: {e}")
        return False

    # Anchor on the actual element we need next, not networkidle.
    try:
        page.wait_for_selector(_ADD_TO_CART_SELECTOR, timeout=10000)
    except Exception:
        # Optional screenshot so the operator can see what page we ended up on.
        if log_step_fn and screenshot_dir and step_num is not None:
            try:
                log_step_fn(step_num, 'drill_through_failed', screenshot_dir, page)
            except Exception:
                pass
        print("[AUTO-BUY] drill-through clicked but Add-to-Cart still not visible")
        return False

    return True


def autobuy_truck(truck_name, truck_url, screenshot_dir=None):
    """Execute auto-buy with proper success/failed/missed detection"""
    if screenshot_dir is None:
        # Default into the persistent workdir volume so screenshots survive
        # container recreate (and so the dashboard's gallery can find them).
        screenshot_dir = os.path.join(
            os.environ.get("WORKDIR", "/app/workdir"),
            "checkout_screenshots",
        )
    _capture_reset(truck_name, truck_url, screenshot_dir)
    print(f"[AUTO-BUY] Starting checkout for: {truck_name}")
    print(f"[AUTO-BUY] URL: {truck_url}")
    print(f"[AUTO-BUY] Max auto-pay: ${MAX_AUTO_PAY}")
    audit_event(
        "purchase.attempt_start",
        truck=truck_name, truck_url=truck_url,
        max_auto_pay=MAX_AUTO_PAY,
    )
    _capture_step("start", {"truck_url": truck_url, "max_auto_pay": MAX_AUTO_PAY})

    os.makedirs(screenshot_dir, exist_ok=True)

    # Acquire the cross-process lock BEFORE launching Playwright so we
    # don't burn a Chromium boot just to abandon. Lock auto-releases at
    # the end of the `with` block (or on process exit). dedup_within
    # also skips this attempt if a previous one for the same truck
    # finished in the last 60 seconds — guards against double-scan storms.
    with exclusive_purchase_lock(truck_url, dedup_within_seconds=60) as (ok, reason):
        if not ok:
            audit_event(
                "purchase.skipped",
                truck=truck_name, truck_url=truck_url,
                reason=reason,
            )
            print(f"[AUTO-BUY] ⏭️  skipping: {reason}")
            return 'SKIPPED', f"locked or recently attempted: {reason}"

        return _autobuy_truck_inner(truck_name, truck_url, screenshot_dir)


def _autobuy_truck_inner(truck_name, truck_url, screenshot_dir):
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(60000)  # 60s timeout (increased from 30s)
            _capture_attach(page)

            # Step 1: Login — domcontentloaded fires once the SPA shell is
            # interactive; the explicit `wait_for_selector` below proves the
            # actual form fields rendered (the only condition that matters
            # for the next step). Switching off `networkidle` here saves 5–15s
            # because Good360 trails analytics beacons for ages after the
            # form is ready.
            # :visible filters out the hidden Sign-up form's duplicate
            # fields; Enter-press submits via the form (the button click
            # gets intercepted by the cookie banner on some viewports).
            print("[AUTO-BUY] Step 1: Logging in...")
            _goto_with_retry(page, sandbox.good360_autobuy_login_url(), wait_until='domcontentloaded', timeout=15000)
            page.wait_for_selector('input[placeholder*="email" i]:visible', state='visible', timeout=15000)
            page.fill('input[placeholder*="email" i]:visible', USERNAME)
            page.fill('input[placeholder*="password" i]:visible', PASSWORD)
            try:
                page.press('input[placeholder*="password" i]:visible', 'Enter')
            except Exception:
                page.click('button[type="submit"]:has-text("Sign in"):visible', force=True, timeout=10000)
            # Positive auth check — Good360's SPA keeps the URL at
            # /sign-in even after success, so we can't gate on the
            # path. The reliable signal is "the password input the
            # user just typed into is no longer in the DOM". Wait up
            # to 15s for that to happen; otherwise the next step
            # would render the catalog in unauthenticated mode (which
            # never shows Add-to-Cart and stalls on "FETCHING DATA…").
            try:
                page.wait_for_function(
                    """() => {
                        const pw = document.querySelector('input[type="password"]')
                                || document.querySelector('input[placeholder*="password" i]');
                        return !pw || pw.offsetParent === null;
                    }""",
                    timeout=15000,
                )
                print("[AUTO-BUY] Login form gone — authenticated")
            except PlaywrightTimeout:
                print("[AUTO-BUY] ⚠️  password input still present — login may have failed")

            log_step(1, 'login', screenshot_dir, page)

            # Step 2: Navigate to truck page. Same domcontentloaded
            # rationale as login — we immediately read page.content() in
            # the next line, which is the actual readiness signal we
            # care about. networkidle here cost 5–15s per attempt for no
            # additional safety.
            print("[AUTO-BUY] Step 2: Going to truck page...")
            _goto_with_retry(page, truck_url, wait_until='domcontentloaded', timeout=12000)

            # Check if truck is available
            page_text = page.content()
            missed, indicator = check_truck_missed(page_text)
            if missed:
                print(f"[AUTO-BUY] ⚠️ TRUCK MISSED - Already sold out: '{indicator}'")
                browser.close()
                return 'MISSED', f"Truck sold out before checkout: {indicator}"

            log_step(3, 'truck_page', screenshot_dir, page)

            # Some truck URLs (e.g. *-placeholder.html category pages)
            # don't render an 'Add to Cart' button directly — they list
            # available trucks as cards and we have to click one to land
            # on the real product detail page. The helper is a no-op
            # when we're already on a detail page.
            if not _ensure_on_product_detail(page, truck_name,
                                              log_step_fn=log_step,
                                              screenshot_dir=screenshot_dir,
                                              step_num=3):
                log_step(4, 'no_add_to_cart', screenshot_dir, page)
                audit_event(
                    "purchase.fail",
                    truck=truck_name, truck_url=truck_url,
                    reason="add_to_cart_unreachable",
                    final_url=page.url,
                )
                send_telegram(f"❌ AUTO-BUY ABORTED — couldn't reach the Add-to-Cart button.\n\n{truck_name}\n{truck_url}\n\nThe URL may be a placeholder/listing page with no purchasable item right now.")
                browser.close()
                return 'FAILED', "Add to Cart not reachable from the truck URL (placeholder page or sold out)"

            # Step 3: Add to cart. Good360's truck flow:
            #   1. Click "Add to cart"
            #   2. A `div.react-confirm-alert-overlay` modal pops up
            #      ("Truck Quote Request" — full truckload can not be
            #      mixed with general products). Press Continue to
            #      empty cart and get a truckload quote.
            #   3. A slide-in `aside[class*="miniCart"]` opens with
            #      the truck and a Checkout button INSIDE the aside.
            # The old `wait_for_selector(Checkout)` here matched a
            # hidden Checkout button somewhere in the DOM, never made
            # it visible, and timed out. We now anchor on the modal
            # and mini-cart, both of which are the daemon's verified
            # selectors for this flow.
            print("[AUTO-BUY] Step 3: Adding to cart...")
            try:
                page.click('button:has-text("Add to cart"), button:has-text("Add to Cart")', timeout=10000)
                time.sleep(3)
                # Truck-quote confirm-alert modal — only pops when the
                # cart already had other items. Fresh contexts skip it.
                cb = page.locator('div.react-confirm-alert-body button:has-text("Continue")')
                try:
                    if cb.count() and cb.first.is_visible(timeout=2500):
                        cb.first.click()
                        print("[AUTO-BUY]   clicked Continue on Truck Quote modal")
                        time.sleep(3)
                except Exception:
                    pass
                # Mini-cart aside lives in the DOM but is hidden until
                # something triggers it. On a fresh context with no
                # prior items, Add-to-Cart silently updates the count
                # without auto-opening the panel — we have to click
                # the cart icon to open it explicitly.
                if not page.locator('aside[class*="miniCart"]').first.is_visible(timeout=1000):
                    try:
                        page.locator('button[class*="cartTrigger"]').first.click(timeout=4000)
                        print("[AUTO-BUY]   opened mini-cart via cart icon")
                        time.sleep(2)
                    except Exception as ee:
                        print(f"[AUTO-BUY]   cart icon click failed: {ee}")
                page.wait_for_selector(
                    'aside[class*="miniCart"]',
                    state='visible', timeout=10000,
                )
                log_step(4, 'add_to_cart', screenshot_dir, page)
            except Exception as e:
                print(f"[AUTO-BUY] Failed to add to cart: {e}")
                browser.close()
                return 'FAILED', f"Could not add to cart: {e}"

            # Cart-contents safety check. Catches the rare-but-catastrophic
            # case where Good360's cart shows a different product than the
            # one we intended to buy (stale state, multi-tab race, an
            # accidental promo upsell). Without this, we'd happily place the
            # order for whatever is in the cart. Match is intentionally
            # generous — only abort when there's no overlap at all.
            try:
                _cart_text = (page.content() or "").lower()
                _name = (truck_name or "").lower().strip()
                # Use the first chunk of the name as a "this had better be there"
                # signal — full names sometimes get truncated in cart UI.
                _probe = _name[:24] if len(_name) >= 24 else _name
                if _probe and _probe not in _cart_text:
                    log_step(4, 'cart_mismatch', screenshot_dir, page)
                    audit_event(
                        "purchase.fail",
                        truck=truck_name, truck_url=truck_url,
                        reason="cart_truck_mismatch",
                        probe=_probe,
                    )
                    print(f"[AUTO-BUY] ❌ Cart does not contain '{_probe}' — refusing to checkout")
                    send_telegram(f"❌ AUTO-BUY ABORTED — cart did not show the expected truck.\n\n{truck_name}\n\nCheck Good360 manually.")
                    browser.close()
                    return 'FAILED', "Cart contents mismatch — refused to checkout"
            except Exception as _cart_err:
                # Don't block on the assertion's own failure — log + continue.
                print(f"[AUTO-BUY] cart-check skipped due to error: {_cart_err}")

            # ============================================================
            # Steps 4-9: 3-step wizard checkout
            # ============================================================
            # Good360's checkout is a single-page accordion wizard:
            #   1. Shipping address  → "Continue to checkout"
            #   2. Checkout questions → "Continue to payment"
            #   3. Payment method    → "Place order"
            # All inputs are React-controlled and dropdowns are
            # react-select (not native <select>). The card-number and
            # CVV inputs live on the page inline (no iframe) under the
            # names cyberSource.cardNumber / cyberSource.securityCode.
            # See [[good360-site-quirks]] in MEMORY.md.

            print("[AUTO-BUY] Step 4: Reaching checkout via mini-cart...")
            try:
                page.wait_for_selector('aside[class*="miniCart"]', state='visible', timeout=10000)
                page.locator('aside[class*="miniCart"] button:has-text("Checkout")').first.click()
                page.wait_for_load_state('domcontentloaded', timeout=10000)
                time.sleep(2)
                log_step(5, 'checkout_start', screenshot_dir, page)
            except Exception as e:
                print(f"[AUTO-BUY] Failed to reach checkout via mini-cart: {e}")
                browser.close()
                return 'FAILED', f"Could not reach checkout page: {e}"

            page_text = page.content()
            missed, indicator = check_truck_missed(page_text)
            if missed:
                print(f"[AUTO-BUY] ⚠️ TRUCK MISSED - Sold out during checkout: '{indicator}'")
                browser.close()
                return 'MISSED', f"Truck sold out during checkout: {indicator}"

            # Resolve the customer org_config (single source of truth
            # for billing-address and checkout answers per
            # [[feedback-customer-data-only]]). cid is set by the
            # monitor; absent for legacy single-org runs which fall
            # back to module-level ANSWERS and env-derived card.
            cid = os.environ.get("GOOD360_CUSTOMER_ID", "").strip()
            cust_cfg = _fetch_org_config_from_dashboard(cid) if cid else None
            cust_ba = (cust_cfg or {}).get("billing_address") or {}
            cust_answers = (cust_cfg or {}).get("checkout_answers") or {}
            cust_card = (cust_cfg or {}).get("card") or {}

            # === REACT FILL HELPER ===
            REACT_FILL_JS = """
                (el, value) => {
                    const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : (el.tagName === 'SELECT'
                            ? window.HTMLSelectElement.prototype
                            : window.HTMLInputElement.prototype);
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, value);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                }
            """

            def pick_by_label(label_substr, value_match, log_label):
                """Open the first unfilled react-select whose surrounding
                label contains `label_substr` and click an option that
                contains `value_match`."""
                unfilled = page.locator(
                    'div[class*="select__control"]:visible:has(div[class*="select__placeholder"])'
                ).all()
                target = None
                for ctl in unfilled:
                    try:
                        ctx_text = ctl.evaluate(
                            """el => {
                                let n = el;
                                for (let i=0; i<6 && n; i++) {
                                    n = n.parentElement; if(!n) break;
                                    const tx = (n.innerText || '').trim();
                                    if (tx && tx.length < 200) {
                                        const lines = tx.split('\\n').map(s=>s.trim())
                                            .filter(s=>s && s!=='Select...' && s!=='*'
                                                       && !s.startsWith('Is required'));
                                        if (lines.length) return lines[0];
                                    }
                                }
                                return '';
                            }"""
                        )
                    except Exception:
                        ctx_text = ''
                    if label_substr.lower() in ctx_text.lower():
                        target = ctl
                        break
                if target is None:
                    print(f"[AUTO-BUY] {log_label}: no dropdown labeled ~{label_substr!r}")
                    return False
                try:
                    target.scroll_into_view_if_needed(timeout=3000)
                    target.click(timeout=5000, force=True)
                    time.sleep(0.7)
                    opts = page.locator('div[class*="select__option"]:visible').all()
                    for o in opts:
                        try:
                            t = (o.inner_text() or '').strip()
                            if value_match.lower() in t.lower():
                                o.click()
                                time.sleep(0.6)
                                print(f"[AUTO-BUY] {log_label} → {t[:70]!r}")
                                return True
                        except Exception:
                            continue
                    page.keyboard.press('Escape')
                    print(f"[AUTO-BUY] {log_label}: no option matched ~{value_match!r}")
                    return False
                except Exception as e:
                    print(f"[AUTO-BUY] {log_label} click error: {e}")
                    try: page.keyboard.press('Escape')
                    except: pass
                    return False

            # === Step 1 → 2: "Continue to checkout" ===
            print("[AUTO-BUY] Step 5: Step 1 → 2 (Continue to checkout)…")
            try:
                page.wait_for_selector('button:has-text("Continue to checkout"):visible', timeout=10000)
                page.locator('button:has-text("Continue to checkout"):visible').first.click()
                time.sleep(3)
            except Exception as e:
                print(f"[AUTO-BUY] no 'Continue to checkout' button: {e}")

            # === Step 2: fill questions + dock/pallet dropdown ===
            print("[AUTO-BUY] Step 6: Step 2 — checkout questions…")
            # Resolve answers: customer record first, then legacy ANSWERS.
            ordered_answers = [
                cust_answers.get('people_helped') or ANSWERS.get('number_of_people') or '',
                cust_answers.get('distribution_method') or ANSWERS.get('distribution_method') or '',
                cust_answers.get('warehouse_address') or ANSWERS.get('warehouse_address') or '',
            ]
            try:
                fields = page.locator('[name^="restriction[question-"]:visible').all()
                text_q = [f for f in fields
                          if f.evaluate('el => el.tagName.toLowerCase()') in ('input', 'textarea')]
                print(f"[AUTO-BUY]   step-2: {len(text_q)} restriction[question-*] inputs")
                for idx, val in enumerate(ordered_answers):
                    if idx >= len(text_q): break
                    if not val:
                        print(f"[AUTO-BUY]   ⚠️ question[{idx}] has no value from customer record")
                        continue
                    try:
                        text_q[idx].evaluate(REACT_FILL_JS, val)
                        print(f"[AUTO-BUY]   question[{idx}] ← {val[:50]!r}")
                    except Exception as ee:
                        print(f"[AUTO-BUY]   question[{idx}] fill failed: {ee}")
            except Exception as e:
                print(f"[AUTO-BUY]   question fill error: {e}")
            # Dock/pallet react-select
            try:
                unfilled = page.locator(
                    'div[class*="select__control"]:visible:has(div[class*="select__placeholder"])'
                ).all()
                for ctl in unfilled:
                    try:
                        ctl.scroll_into_view_if_needed(timeout=2000)
                        ctl.click(timeout=4000, force=True); time.sleep(0.6)
                        for opt in page.locator('div[class*="select__option"]:visible').all():
                            t = (opt.inner_text() or '').lower()
                            if any(k in t for k in ('loading dock','dock','pallet jack','forklift','yes')):
                                opt.click(); time.sleep(0.4)
                                print(f"[AUTO-BUY]   dock/pallet → {opt.inner_text().strip()[:60]!r}")
                                break
                        else:
                            page.keyboard.press('Escape'); continue
                        break  # first successful pick is enough
                    except Exception:
                        try: page.keyboard.press('Escape')
                        except: pass
                        continue
            except Exception as e:
                print(f"[AUTO-BUY]   dock/pallet error: {e}")

            # === Step 2 → 3: "Continue to payment" ===
            print("[AUTO-BUY] Step 7: Step 2 → 3 (Continue to payment)…")
            try:
                page.wait_for_selector('button:has-text("Continue to payment"):visible', timeout=10000)
                page.locator('button:has-text("Continue to payment"):visible').first.click()
                time.sleep(3)
            except Exception as e:
                print(f"[AUTO-BUY]   'Continue to payment' click failed: {e}")

            # Pull the total off the page now that step 3 is rendered.
            page_text = page.content()
            admin_fee, shipping_fee, total = extract_total(page_text)
            print(f"[AUTO-BUY] Admin fee: ${admin_fee}, Shipping: ${shipping_fee}, Total: ${total}")
            if total > MAX_AUTO_PAY:
                log_step(7, 'over_limit_total', screenshot_dir, page)
                audit_event(
                    "purchase.manual",
                    truck=truck_name, truck_url=truck_url,
                    total=total, max_auto_pay=MAX_AUTO_PAY,
                    admin_fee=admin_fee, shipping_fee=shipping_fee,
                    reason="total_exceeds_limit",
                )
                msg = f"⚠️ MANUAL PURCHASE REQUIRED\n\n{truck_name}\nTotal: ${total} (exceeds ${MAX_AUTO_PAY} limit)\n\n🔗 {truck_url}"
                send_telegram(msg)
                browser.close()
                return 'MANUAL', f"Total ${total} exceeds limit ${MAX_AUTO_PAY}"

            log_step(8, 'payment_page', screenshot_dir, page)

            # === Step 3: expand + fill payment ===
            print("[AUTO-BUY] Step 8: Step 3 — payment method…")
            try:
                step3_head = page.locator(
                    'div[class*="collapseHead"]:has-text("Payment method"), '
                    'h3:has-text("Payment method"), '
                    'div:has-text("Add billing information")'
                ).first
                if step3_head.count() and step3_head.is_visible(timeout=2000):
                    try:
                        step3_head.click(timeout=4000); time.sleep(1)
                        print("[AUTO-BUY]   expanded step 3 (Payment method)")
                    except Exception as ee:
                        print(f"[AUTO-BUY]   step-3 expand failed: {ee}")
            except Exception as e:
                print(f"[AUTO-BUY]   step-3 expand error: {e}")

            # Saved billing address — match by customer's primary
            # card billing zip. Per [[feedback-customer-data-only]] we
            # never fall back to a hardcoded value here.
            billing_zip = cust_ba.get('postcode') or ''
            if not billing_zip:
                print("[AUTO-BUY] ⚠️ no billing zip from customer record — saved-address pick will fail")
            picked_addr = False
            if billing_zip:
                picked_addr = pick_by_label('billing address', billing_zip, 'billing.address')
            if not picked_addr and cust_ba.get('street'):
                token = cust_ba['street'].split()[0]
                picked_addr = pick_by_label('billing address', token, 'billing.address')
            if not picked_addr:
                log_step(10, 'billing_unselected', screenshot_dir, page)
                audit_event(
                    "purchase.fail",
                    truck=truck_name, truck_url=truck_url,
                    reason="billing_no_option_matched",
                )
                send_telegram(f"❌ AUTO-BUY ABORTED — couldn't pick saved billing address (zip={billing_zip!r}).\n\n{truck_name}")
                browser.close()
                return 'FAILED', "Billing address option not found"

            # Card type + expiry. Priority:
            #   sandbox mode OR AUTOBUY_USE_SANDBOX_CARD=true
            #       → SANDBOX_CARD_* env (never the real PAN)
            #   else  → customer card from QuickBeed, env as fallback
            # The AUTOBUY_USE_SANDBOX_CARD override exists so an
            # integration test can exercise the real Good360 form-fill
            # path with a sandbox card without flipping SANDBOX_MODE
            # (which would swap URLs to sandbox-360.netlify.app and skip
            # the real site entirely). See [[feedback-no-real-card-charges]].
            card_type = (cust_card.get('type') or '').strip() or 'visa'
            card_number = ''
            card_expiry_raw = ''
            card_cvv = ''
            force_sandbox_card = (
                os.environ.get('AUTOBUY_USE_SANDBOX_CARD', '').lower()
                in ('1', 'true', 'yes')
            )
            try:
                env_n, env_exp, env_cvv, _, _ = sandbox.env_card_fields('CARD_HOPE4HUMANITY')
                if sandbox.is_sandbox() or force_sandbox_card:
                    sandbox_n = os.environ.get('SANDBOX_CARD_NUMBER') or env_n
                    sandbox_e = os.environ.get('SANDBOX_CARD_EXPIRY') or env_exp
                    sandbox_c = os.environ.get('SANDBOX_CARD_CVV')    or env_cvv
                    card_number, card_expiry_raw, card_cvv = sandbox_n, sandbox_e, sandbox_c
                    src = 'AUTOBUY_USE_SANDBOX_CARD override' if force_sandbox_card else 'sandbox mode'
                    print(f"[AUTO-BUY] {src} — using SANDBOX_CARD_* "
                          f"(****{(card_number or '')[-4:]})")
                else:
                    card_number     = cust_card.get('number')  or env_n
                    card_expiry_raw = cust_card.get('expiry')  or env_exp
                    card_cvv        = cust_card.get('cvv')     or env_cvv
            except Exception:
                pass
            if not (card_number and card_expiry_raw and card_cvv):
                browser.close()
                return 'FAILED', 'Card details unavailable from customer record or env — refusing to submit empty payment form'
            exp = (card_expiry_raw or '').strip().zfill(4)
            exp_m = exp[:2] if len(exp) >= 4 else ''
            exp_y = ('20' + exp[2:4]) if len(exp) >= 4 else ''

            pick_by_label('card type', card_type, 'card.type')
            if exp_m: pick_by_label('expiry month', exp_m, 'card.exp_month')
            if exp_y: pick_by_label('expiry year', exp_y, 'card.exp_year')

            # Payment-method radio (chcybersource)
            try:
                for r in page.locator('input[type="radio"]:visible').all():
                    val = (r.get_attribute('value') or '').lower()
                    if any(k in val for k in ('card','cyber','visa','credit')):
                        try: r.check(); break
                        except Exception: pass
            except Exception:
                pass

            # Fill cyberSource.cardNumber + cyberSource.securityCode.
            # No screenshot is taken between fill and Place Order — the
            # form briefly contains the full PAN.
            try:
                cn = page.locator('input[name="cyberSource.cardNumber"]:visible')
                if cn.count():
                    cn.first.evaluate(REACT_FILL_JS, card_number)
                    print(f"[AUTO-BUY]   cyberSource.cardNumber filled (****{card_number[-4:]})")
                cv = page.locator('input[name="cyberSource.securityCode"]:visible')
                if cv.count():
                    cv.first.evaluate(REACT_FILL_JS, card_cvv)
                    print("[AUTO-BUY]   cyberSource.securityCode filled")
            except Exception as e:
                print(f"[AUTO-BUY]   cyberSource fill error: {e}")
            finally:
                card_number = card_expiry_raw = card_cvv = ""

            log_step(10, 'before_place_order', screenshot_dir, page)

            # === Place order ===
            print("[AUTO-BUY] Step 9: Placing order...")
            try:
                page.click('button:has-text("Place order"), button:has-text("Place Order"), button:has-text("Submit")', timeout=10000)
                # Don't use a bare time.sleep(5) — confirmation pages can take
                # 10-30s on slow days, and the previous fixed wait would snap
                # the in-flight page and false-FAIL real successes. Poll the
                # DOM for any of our success indicators (or a confirmation-
                # shaped URL) and return as soon as one appears.
                try:
                    page.wait_for_function(
                        """() => {
                            const t = ((document.body && document.body.innerText) || '').toLowerCase();
                            if (/thank you for your (order|purchase)|order confirmed|order number|order\\s*#|your order has been placed|order receipt|payment successful|checkout complete/.test(t)) return true;
                            return /\\/(order|confirmation|success|thank)/.test(location.pathname.toLowerCase());
                        }""",
                        timeout=30000,
                    )
                    print("[AUTO-BUY] Confirmation indicator detected after place-order")
                except PlaywrightTimeout:
                    # Either a real failure or the indicators don't match the
                    # current site's wording. Fall through — the next check
                    # captures whatever page is on screen now.
                    print("[AUTO-BUY] No confirmation indicator within 30s — capturing current page state")
            except Exception as e:
                print(f"[AUTO-BUY] Place order error: {e}")
                browser.close()
                return 'FAILED', f"Could not click Place Order: {e}"

            log_step(11, 'after_place_order', screenshot_dir, page)

            # Step 10: VERIFY ORDER COMPLETION
            print("[AUTO-BUY] Step 10: Verifying order...")
            page_text = page.content()
            page_url  = page.url

            # Check for TRUE SUCCESS — pass URL so a confirmation-shaped
            # path counts as success even if the body copy hasn't matched
            # one of our text indicators yet.
            is_confirmed, indicator = check_order_confirmation(page_text, page_url)

            if is_confirmed:
                confirmation_number = extract_confirmation_number(page_text)
                print(f"[AUTO-BUY] ✅ ORDER CONFIRMED! Indicator: '{indicator}' conf#={confirmation_number or '—'}")
                audit_event(
                    "purchase.success",
                    truck=truck_name, truck_url=truck_url,
                    total=total, indicator=indicator,
                    confirmation_number=confirmation_number,
                    final_url=page_url,
                )
                conf_line = f"\nOrder #: {confirmation_number}" if confirmation_number else ""
                success_msg = f"✅ AUTO-BUY COMPLETE!\n\n{truck_name}\nTotal: ${total}{conf_line}\n\nPurchase successful - check your email for confirmation!\n\n— E-Comsetter Auto-Buy"
                send_telegram(success_msg)
                browser.close()
                detail = f"Order confirmed: {indicator}"
                if confirmation_number:
                    detail += f" (#{confirmation_number})"
                return 'SUCCESS', detail

            # Check for CARD DECLINED. Strong end-to-end signal — it
            # means Place Order really submitted to the payment
            # gateway. In sandbox tests this is the *expected* outcome
            # and proves the autobuy flow works without spending money.
            declined, decline_indicator = check_card_declined(page_text)
            if declined:
                print(f"[AUTO-BUY] 💳 CARD_DECLINED by payment processor: '{decline_indicator}'")
                audit_event(
                    "purchase.card_declined",
                    truck=truck_name, truck_url=truck_url,
                    total=total, indicator=decline_indicator,
                    final_url=page_url,
                )
                send_telegram(
                    f"💳 AUTO-BUY: Card declined\n\n{truck_name}\nTotal: ${total}\n\n"
                    f"CyberSource rejected the card with: {decline_indicator!r}.\n"
                    f"This is the expected outcome in sandbox tests — proves the "
                    f"flow reached the payment processor.\n\n— E-Comsetter Auto-Buy"
                )
                browser.close()
                return 'CARD_DECLINED', f"Card declined by payment processor: {decline_indicator}"

            # Check for TRUCK MISSED
            missed, indicator = check_truck_missed(page_text)
            if missed:
                print(f"[AUTO-BUY] ⚠️ TRUCK MISSED - Sold out during checkout: '{indicator}'")
                audit_event(
                    "purchase.missed",
                    truck=truck_name, truck_url=truck_url,
                    total=total, indicator=indicator,
                )
                browser.close()
                return 'MISSED', f"Truck sold out: {indicator}"

            # Otherwise it's a FAILURE
            print("[AUTO-BUY] ❌ ORDER NOT CONFIRMED - No success indicators found")
            audit_event(
                "purchase.fail",
                truck=truck_name, truck_url=truck_url,
                total=total, final_url=page_url,
                reason="no_success_indicator",
            )
            fail_msg = f"❌ AUTO-BUY FAILED\n\n{truck_name}\nTotal: ${total}\n\nOrder could not be completed. Manual purchase may be needed.\n\n🔗 {truck_url}\n\n— E-Comsetter Auto-Buy"
            send_telegram(fail_msg)
            browser.close()
            return 'FAILED', "Order confirmation not found after Place Order"

    except PlaywrightTimeout as e:
        error_msg = f"Timeout: {str(e)[:100]}"
        print(f"[AUTO-BUY] ❌ {error_msg}")
        audit_event(
            "purchase.fail",
            truck=truck_name, truck_url=truck_url,
            reason="playwright_timeout", error=error_msg,
        )
        send_telegram(f"❌ AUTO-BUY TIMEOUT\n\n{truck_name}\n{error_msg}\n\n— E-Comsetter")
        return 'FAILED', error_msg

    except Exception as e:
        error_msg = f"Error: {str(e)[:100]}"
        print(f"[AUTO-BUY] ❌ {error_msg}")
        traceback.print_exc()
        audit_event(
            "purchase.fail",
            truck=truck_name, truck_url=truck_url,
            reason="exception", error=error_msg,
            exception_type=type(e).__name__,
        )
        send_telegram(f"❌ AUTO-BUY ERROR\n\n{truck_name}\n{error_msg}\n\n— E-Comsetter")
        return 'FAILED', error_msg

if __name__ == "__main__":
    if not feature_flags.auto_buy_enabled():
        print("BLOCKED: auto-buy disabled in this environment (ENABLE_AUTO_BUY=false)")
        sys.exit(2)

    if len(sys.argv) < 3:
        print("Usage: python good360_autobuy.py <truck_name> <truck_url>")
        sys.exit(1)

    truck_name = sys.argv[1]
    truck_url = sys.argv[2]

    result, message = autobuy_truck(truck_name, truck_url)
    print(f"\n[AUTO-BUY] RESULT: {result} - {message}")

    # Record the final outcome into the capture so the dashboard can show
    # status without re-parsing stdout. atexit would still write the file,
    # but the outcome/message fields would be empty.
    _capture_finalize(outcome=result, message=message)

    # Exit codes: 0=success, 1=failed, 2=missed, 3=manual
    exit_codes = {'SUCCESS': 0, 'FAILED': 1, 'MISSED': 2, 'MANUAL': 3,
                  'CARD_DECLINED': 6}
    sys.exit(exit_codes.get(result, 1))
