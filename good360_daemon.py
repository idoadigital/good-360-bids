#!/usr/bin/env python3
"""Good360 Persistent Browser Auto-Buy Daemon v2"""

import json
import logging
import os
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytz
from playwright.sync_api import sync_playwright

import config as _cfg
try:
    import feature_flags
except ImportError:
    # Stale image/mount combo (old container recreated from a pinned compose
    # file without the feature_flags.py mount). Same semantics, env-only.
    import os as _ff_os
    import types as _ff_t

    def _ff_flag(name):
        return _ff_os.environ.get(name, "true").strip().lower() not in (
            "false", "0", "no", "off")

    feature_flags = _ff_t.SimpleNamespace(
        flag_enabled=lambda name, default=True: _ff_flag(name),
        auto_buy_enabled=lambda: _ff_flag("ENABLE_AUTO_BUY"),
        url_scanning_enabled=lambda: _ff_flag("ENABLE_URL_SCANNING"),
        notifications_enabled=lambda: _ff_flag("ENABLE_NOTIFICATIONS"),
        notifications_blocked_msg=lambda ch: (
            f"[NOTIFICATIONS DISABLED] {ch} send skipped "
            "(ENABLE_NOTIFICATIONS=false in this environment)"),
    )
import sandbox  # sandbox-mode URL routing
from purchase_lock import exclusive_purchase_lock

WORKDIR = os.environ.get("WORKDIR", "/a0/usr/workdir")
SCREENSHOT_DIR = f"{WORKDIR}/browser_screenshots"
CAPTURE_DIR = f"{WORKDIR}/checkout_captures"
LOG_FILE = f"{WORKDIR}/good360_daemon.log"
STATE_FILE = f"{WORKDIR}/good360_daemon_state.json"

# Per-checkout capture buffer. The daemon's pages are long-lived so we
# attach listeners once per page (in get_or_create_context) and reset
# this dict at the start of each checkout. Listeners only write into
# whatever capture is "current" — meaning nothing piles up between runs.
_DAEMON_CAPTURE: dict = {}
_CAPTURE_BODY_CAP = 16 * 1024
_CAPTURE_NET_LIMIT = 200
_CAPTURE_CONSOLE_LIMIT = 200
import re as _re   # noqa: E402

def _capture_reset(org_key: str, truck_name: str, truck_url: str) -> str:
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe = _re.sub(r"[^A-Za-z0-9_.-]+", "_", f"{org_key}_{truck_name}")[:80]
    path = f"{CAPTURE_DIR}/{ts}_{safe}_daemon.json"
    _DAEMON_CAPTURE.clear()
    _DAEMON_CAPTURE.update({
        "started_at": datetime.now().isoformat(),
        "engine":     "daemon",
        "org_key":    org_key,
        "truck_name": truck_name,
        "truck_url":  truck_url,
        "network":    [],
        "console":    [],
        "steps":      [],
        "outcome":    None,
        "message":    None,
        "path":       path,
    })
    return path

def _capture_attach(page):
    """One-time listener registration per page. Writes into whatever
    _DAEMON_CAPTURE is current — no-op when there's no active capture."""
    def on_response(resp):
        if not _DAEMON_CAPTURE.get("path"):
            return
        try:
            body = ""
            ct = (resp.headers.get("content-type") or "").lower()
            if any(k in ct for k in ("json","text","html","xml","javascript","form")):
                try:
                    raw = resp.body()
                    body = raw[:_CAPTURE_BODY_CAP].decode("utf-8", errors="replace")
                    if len(raw) > _CAPTURE_BODY_CAP:
                        body += f"\n…[truncated {len(raw)-_CAPTURE_BODY_CAP} bytes]"
                except Exception:
                    body = ""
            _DAEMON_CAPTURE["network"].append({
                "ts":           datetime.now().isoformat(timespec="milliseconds"),
                "method":       resp.request.method,
                "url":          resp.url,
                "status":       resp.status,
                "content_type": resp.headers.get("content-type") or "",
                "size":         len(body),
                "body":         body,
            })
            if len(_DAEMON_CAPTURE["network"]) > _CAPTURE_NET_LIMIT:
                del _DAEMON_CAPTURE["network"][: len(_DAEMON_CAPTURE["network"]) - _CAPTURE_NET_LIMIT]
        except Exception:
            pass
    def on_console(msg):
        if not _DAEMON_CAPTURE.get("path"):
            return
        try:
            _DAEMON_CAPTURE["console"].append({
                "ts":   datetime.now().isoformat(timespec="milliseconds"),
                "type": msg.type,
                "text": msg.text[:2000],
            })
            if len(_DAEMON_CAPTURE["console"]) > _CAPTURE_CONSOLE_LIMIT:
                del _DAEMON_CAPTURE["console"][: len(_DAEMON_CAPTURE["console"]) - _CAPTURE_CONSOLE_LIMIT]
        except Exception:
            pass
    def on_pageerror(err):
        if not _DAEMON_CAPTURE.get("path"):
            return
        try:
            _DAEMON_CAPTURE["console"].append({
                "ts":   datetime.now().isoformat(timespec="milliseconds"),
                "type": "pageerror",
                "text": str(err)[:2000],
            })
        except Exception:
            pass
    page.on("response", on_response)
    page.on("console",  on_console)
    page.on("pageerror", on_pageerror)

def _capture_step(label: str, extra: dict | None = None):
    if not _DAEMON_CAPTURE.get("path"):
        return
    _DAEMON_CAPTURE.setdefault("steps", []).append({
        "ts":    datetime.now().isoformat(timespec="milliseconds"),
        "label": label,
        **(extra or {}),
    })

def _capture_finalize(page, outcome: str, message: str) -> str | None:
    if not _DAEMON_CAPTURE.get("path"):
        return None
    _DAEMON_CAPTURE["finished_at"] = datetime.now().isoformat()
    _DAEMON_CAPTURE["outcome"] = outcome
    _DAEMON_CAPTURE["message"] = message
    try:
        _DAEMON_CAPTURE["final_url"]  = page.url
        _DAEMON_CAPTURE["final_html"] = page.content()[:64 * 1024]
    except Exception:
        pass
    path = _DAEMON_CAPTURE["path"]
    try:
        with open(path, "w") as f:
            json.dump(_DAEMON_CAPTURE, f, indent=2, default=str)
        log.info(f"[capture] wrote {path}")
        # Clear `path` so any in-flight listeners stop writing to this buffer
        # (the next checkout will reset and re-arm).
        _DAEMON_CAPTURE["path"] = None
        return path
    except Exception as e:
        log.warning(f"[capture] write failed: {e}")
        return None
# GOOD360_HOME used to be a hardcoded constant; it now resolves at call time
# via sandbox.good360_browse_url() so a sandbox toggle takes effect on the
# next page load without a daemon restart.
DAEMON_PORT = 5002

logging.basicConfig(level=logging.INFO,
    format='%(asctime)s [DAEMON] %(message)s',
    handlers=[logging.FileHandler(LOG_FILE), logging.StreamHandler()])
log = logging.getLogger('daemon')

def now_et():
    return datetime.now(pytz.timezone('America/New_York'))

# Good360 renders the cart button as:
#   <button class="button-root-<hash> clickable-root-<hash>
#                   button-high-<hash> button-green-<hash>">
#     <span class="button-content-<hash>"><span>Add to cart</span></span>
#   </button>
# The text is nested inside two spans; `:has-text()` matches descendant
# text so the text selectors still work. The CSS-module hash suffixes
# (-1qv etc) change on every rebuild, but the prefixes (button-green,
# clickable-root, button-high) survive, so they're worth matching as a
# secondary signal alongside the text. Order is most-robust first.
_DAEMON_ADD_TO_CART_SELECTOR = (
    'button:has-text("Add to cart"), '
    'button:has-text("Add to Cart"), '
    'button:has(span:text-is("Add to cart")), '
    'button:has(span:text-is("Add to Cart")), '
    'button[class*="button-green"]:has-text("Add"), '
    'button[class*="clickable-root"]:has-text("Add to"), '
    '[class*="add-to-cart"], '
    '[data-action="add-to-cart"]'
)


def _hit_login_wall(page) -> bool:
    """Return True if the current page is in a logged-out state.

    Good360 doesn't render "please log in" body text — it just hides the
    Add-to-Cart and shows a chrome-level Login/Sign-in button instead.
    That button's presence is the only reliable logged-out signal on
    catalog pages; without checking it, the daemon's liveness probe
    silently passes and we navigate to the truck URL unauthenticated
    (the exact failure mode that's been producing 'Add to Cart not
    reachable' on Berneitha's Softlines URL)."""
    try:
        # URL redirect to sign-in / login: definitive logged-out signal.
        cur_url = (page.url or "").lower()
        if any(s in cur_url for s in ("/sign-in", "/sign_in", "/marketplace/sign-in", "/login")):
            return True
        # Visible login form (in any of its placeholder shapes).
        login_inputs = (
            'input[placeholder*="email" i]:visible, '
            'input[placeholder*="username" i]:visible, '
            'input[type="email"]:visible, '
            'input[name="email"]:visible, '
            'input[name="username"]:visible, '
            'input[type="password"]:visible'
        )
        if page.locator(login_inputs).count() > 0:
            return True
        # Visible Login / Sign in button in the page chrome — Good360's
        # nav swaps this for an account/Donor button when authenticated,
        # so its presence is the cleanest logged-out indicator. Anchor
        # tags with login-shaped hrefs count too.
        login_chrome = (
            'button:has-text("Login"):visible, '
            'button:has-text("Log in"):visible, '
            'button:has-text("Sign in"):visible, '
            'a:has-text("Login"):visible, '
            'a:has-text("Log in"):visible, '
            'a:has-text("Sign in"):visible, '
            'a[href*="/sign-in"]:visible, '
            'a[href*="/login"]:visible'
        )
        if page.locator(login_chrome).count() > 0:
            return True
        body = (page.inner_text('body') or "").lower()
    except Exception:
        return False
    walls = (
        "you must first login",
        "you must log in",
        "you must sign in",
        "please log in",
        "please sign in",
        "log in to view",
        "log in to continue",
        "sign in to view",
        "sign in to continue",
        "session has expired",
        "session expired",
        "your session has expired",
        "login required",
    )
    return any(w in body for w in walls)


def _ensure_on_product_detail_daemon(page, truck_name: str) -> tuple[bool, str]:
    """Mirror of the autobuy.py helper. If we landed on a Good360
    placeholder/listing page (e.g. /marketplace/*-placeholder.html), the
    Add-to-Cart button only renders after clicking a product card.
    Drills through to the real detail page.

    Returns (ok, reason). When ok=False, `reason` carries a short
    operator-actionable explanation so the dashboard surfaces the actual
    cause instead of a generic message."""
    import re as _re

    # Give the SPA a moment to actually render after navigation. Good360's
    # marketplace is a JS-heavy app — the initial HTML is mostly shell and
    # the real product cards / Add-to-Cart only appear after hydration.
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

    # Fast path: wait up to 8s for the Add-to-Cart button to appear.
    # On a logged-in product page the button typically renders within
    # 1–3s; an 8s ceiling tolerates slow networks without holding up
    # the legitimate "not on a product page" path for long.
    try:
        page.wait_for_selector(_DAEMON_ADD_TO_CART_SELECTOR, state="visible", timeout=8000)
        return True, "add_to_cart_visible"
    except Exception:
        pass

    final_url = (page.url or "").lower()

    # Diagnostic snapshot of the page we DID land on. Helps the AI
    # diagnosis path tell the difference between "wrong URL" vs
    # "wrong selectors" vs "logged-out wall".
    try:
        a_count   = page.locator('a:visible').count()
        btn_count = page.locator('button:visible').count()
        log.info(f"[daemon] drill-through diagnostic: url={final_url} visible_anchors={a_count} visible_buttons={btn_count}")
        # Sample first 8 visible anchor texts so we can adapt selectors.
        sample = []
        for i in range(min(a_count, 8)):
            try:
                t = page.locator('a:visible').nth(i).inner_text().strip()
                h = page.locator('a:visible').nth(i).get_attribute('href') or ''
                if t or h:
                    sample.append(f"  [{i}] {t[:40]!r} → {h[:80]}")
            except Exception:
                pass
        if sample:
            log.info("[daemon] visible anchors sample:\n" + "\n".join(sample))
    except Exception as e:
        log.warning(f"[daemon] drill-through diagnostic logging failed: {e}")

    # Best-effort screenshot of the failure state so operators can see
    # exactly what page we're stuck on without re-running.
    try:
        os.makedirs(SCREENSHOT_DIR, exist_ok=True)
        shot_name = f"{SCREENSHOT_DIR}/{int(time.time())}_drill_failed.png"
        page.screenshot(path=shot_name, full_page=True)
        log.info(f"[daemon] saved drill-through failure screenshot: {shot_name}")
    except Exception:
        pass

    # Redirect-to-marketplace-home check. Good360 redirects unresolvable
    # placeholder URLs to /marketplace/ — when that happens the truck
    # name simply isn't directly addressable; the operator needs to
    # supply a real product URL or use search.
    if _re.search(r'/marketplace/?$', final_url):
        log.warning("[daemon] truck URL redirected to /marketplace/ home — placeholder URL does not resolve to a product")
        return False, "url_redirects_to_home"

    log.info(f"[daemon] no Add-to-Cart on landing page — drilling through for {truck_name!r}")
    # WIDER selectors: marketplace anchors, product-card patterns, and any
    # visible anchor whose text matches a distinctive token from the
    # truck name (the most reliable signal for a SPA that doesn't expose
    # a class= we know about).
    candidates = page.locator(
        'main a[href*="/marketplace/"]:visible, '
        'article a[href*="/marketplace/"]:visible, '
        'a.product-card:visible, '
        '.product-card a:visible, '
        '.product-tile a:visible, '
        '[class*="product"] a:visible, '
        '[class*="ProductCard"] a:visible, '
        '[class*="card"] a[href*="/marketplace/"]:visible'
    )
    try:
        n = min(candidates.count(), 30)
    except Exception:
        n = 0

    # Fallback: ALL visible anchors whose text contains a distinctive
    # token from the truck name. Use as a second pass if structured
    # selectors find nothing.
    name_lc = (truck_name or "").lower()
    tokens = [w for w in _re.split(r'[\s\-_,]+', name_lc) if len(w) >= 4]

    if n == 0 and tokens:
        all_anchors = page.locator('a:visible')
        try:
            total = min(all_anchors.count(), 100)
        except Exception:
            total = 0
        for i in range(total):
            try:
                t = all_anchors.nth(i).inner_text().strip().lower()
            except Exception:
                continue
            if any(tok in t for tok in tokens):
                try:
                    log.info(f"[daemon] fallback match on visible anchor: {t[:80]!r}")
                    all_anchors.nth(i).click()
                except Exception as e:
                    log.warning(f"[daemon] fallback click failed: {e}")
                    return False, "fallback_click_failed"
                try:
                    page.wait_for_selector(_DAEMON_ADD_TO_CART_SELECTOR, timeout=10000)
                    return True, "drilled_via_text_match"
                except Exception:
                    return False, "no_add_to_cart_after_text_match"
        return False, "no_product_links_match_name"

    if n == 0:
        return False, "no_product_links_on_page"

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
        chosen_idx = 0

    try:
        candidates.nth(chosen_idx).click()
    except Exception as e:
        log.warning(f"[daemon] drill-through click failed: {e}")
        return False, "structured_click_failed"
    try:
        page.wait_for_selector(_DAEMON_ADD_TO_CART_SELECTOR, timeout=10000)
        return True, "drilled_via_structured"
    except Exception:
        return False, "no_add_to_cart_after_drill"


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.contexts = {}
        self.lock = threading.Lock()
        self.running = True

    def start(self):
        log.info("Starting Playwright...")
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.launch(
            headless=True,
            args=['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--fast-start']
        )
        log.info("Browser launched successfully")

    def get_or_create_context(self, org_key):
        """Get existing context or create new one with persistent storage"""
        with self.lock:
            if org_key in self.contexts:
                ctx = self.contexts[org_key]
                try:
                    ctx['page'].evaluate('1+1')
                    ctx['last_activity'] = time.time()
                    return ctx
                except:
                    log.info(f"[{org_key}] Context dead, recreating...")
                    try: ctx['context'].close()
                    except: pass
                    del self.contexts[org_key]

            log.info(f"[{org_key}] Creating new browser context")
            ctx_dir = f"{WORKDIR}/browser_data/{org_key}"
            os.makedirs(ctx_dir, exist_ok=True)

            # Use storage_state for cookie persistence
            storage_file = f"{ctx_dir}/storage_state.json"
            context_opts = {
                'viewport': {'width': 1280, 'height': 720},
                'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            }
            if os.path.exists(storage_file):
                context_opts['storage_state'] = storage_file
                log.info(f"[{org_key}] Loading saved cookies from {storage_file}")

            context = self.browser.new_context(**context_opts)
            page = context.new_page()
            page.set_default_timeout(15000)
            _capture_attach(page)

            ctx = {
                'context': context,
                'page': page,
                'org_key': org_key,
                'logged_in': False,
                'last_activity': time.time(),
                'created_at': time.time(),
                'storage_file': storage_file
            }
            self.contexts[org_key] = ctx
            return ctx

    def save_session(self, ctx):
        """Save browser session cookies"""
        try:
            ctx['context'].storage_state(path=ctx['storage_file'])
            log.info(f"[{ctx['org_key']}] Session saved")
        except Exception as e:
            log.warning(f"[{ctx['org_key']}] Session save failed: {e}")

    def ensure_logged_in(self, ctx, org_config, force=False):
        # Always probe the live session — the in-memory `logged_in` flag is
        # not enough. Good360 silently expires the cookie server-side, and
        # we'd otherwise sail into the checkout flow against a login page,
        # producing FAILED with cryptic selector timeouts on Add-to-Cart.
        email = org_config.get('good360_email', '')
        password = org_config.get('good360_password', '')
        # Loud-log the credentials being used (email only — never password)
        # so the operator can confirm in the daemon log that the customer's
        # own Good360 account is what we're signing in as.
        if email:
            log.info(f"[{ctx['org_key']}] credentials in use: login_as={email} pw_len={len(password)} force={force}")
        else:
            log.warning(f"[{ctx['org_key']}] no good360_email in org_config — login will fail")
        page = ctx['page']
        try:
            # Liveness signal: route through _hit_login_wall, which now
            # correctly detects Good360's "Login" button in the chrome
            # — the prior `input[placeholder*=email]` check missed it
            # because the catalog pages don't render the form inline.
            # Skip the probe entirely when `force=True` — the caller has
            # already wiped cookies and explicitly wants a fresh form
            # submission. Trusting the probe under force has bitten us
            # because localStorage from prior sessions can keep the SPA
            # in a half-logged-in state where _hit_login_wall sees no
            # wall but the actual catalog requests fail.
            if not force:
                page.goto(sandbox.good360_browse_url(), wait_until='domcontentloaded', timeout=20000)
                # Wait for the SPA to render its auth chrome (Login button
                # vs Donor button) before judging. 1s was too short on
                # cold-context runs.
                try:
                    page.wait_for_selector(
                        'button:has-text("Login"):visible, '
                        'button:has-text("Sign in"):visible, '
                        'a:has-text("Sign in"):visible, '
                        'button:has-text("Donor"):visible, '
                        '[class*="account"]:visible',
                        timeout=6000,
                    )
                except Exception:
                    pass
                if not _hit_login_wall(page):
                    if not ctx['logged_in']:
                        log.info(f"[{ctx['org_key']}] Already logged in (cookie/storage)")
                    ctx['logged_in'] = True
                    return True
            else:
                log.info(f"[{ctx['org_key']}] force=True — bypassing liveness probe, driving sign-in form unconditionally")

            if ctx['logged_in']:
                log.warning(f"[{ctx['org_key']}] Session expired silently — re-logging in")
                ctx['logged_in'] = False

            # Drive the sign-in flow. domcontentloaded instead of
            # networkidle — Good360 has trailing analytics that never
            # idle, so networkidle was burning the whole 30s before the
            # email-input wait even started. Broader selector covers
            # placeholder-styled inputs AND native type=email inputs.
            email_sel = (
                'input[placeholder*="email" i]:visible, '
                'input[type="email"]:visible, '
                'input[name="email"]:visible'
            )
            pw_sel = (
                'input[placeholder*="password" i]:visible, '
                'input[type="password"]:visible, '
                'input[name="password"]:visible'
            )
            log.info(f"[{ctx['org_key']}] Logging in as {email}…")
            # Cold-context navigation: the SPA bundle is large and the
            # email input can take 15-25s to render on first paint.
            # Retry once if the wait times out so a slow first paint
            # doesn't fail the whole checkout.
            form_ready = False
            for attempt in (1, 2):
                try:
                    page.goto(sandbox.good360_autobuy_login_url(), wait_until='domcontentloaded', timeout=25000)
                except Exception as e:
                    log.warning(f"[{ctx['org_key']}] sign-in nav attempt {attempt} failed: {e}")
                try:
                    page.wait_for_selector(email_sel, state='visible', timeout=25000)
                    form_ready = True
                    break
                except Exception as e:
                    log.warning(f"[{ctx['org_key']}] login form not visible (attempt {attempt}): {e}")
                    if attempt == 1:
                        time.sleep(2)
            if not form_ready:
                log.error(f"[{ctx['org_key']}] login form never rendered after 2 attempts")
                ctx['logged_in'] = False
                return False
            page.fill(email_sel, email)
            page.fill(pw_sel, password)
            try:
                page.press(pw_sel, 'Enter')
            except Exception:
                page.click('button[type="submit"]:has-text("Sign in"):visible', timeout=10000, force=True)
            try:
                page.wait_for_load_state('domcontentloaded', timeout=10000)
            except Exception:
                pass
            time.sleep(2)

            # Positive auth check: is the password form gone? Good360 uses
            # client-side routing — the URL stays at /sign-in even after a
            # successful login (the title flips to "Account overview" but
            # the path doesn't change). So we can't gate on URL. The
            # reliable signal is "the visible password input the user just
            # typed into is no longer in the DOM" — the form unmounts when
            # the auth state flips. The truck-URL login-wall recovery
            # downstream catches the rare case where the session lapses
            # between here and the actual product page.
            still_has_pw_form = page.locator(
                'input[type="password"]:visible, '
                'input[placeholder*="password" i]:visible'
            ).count() > 0
            still_has_signin_button = page.locator(
                'button[type="submit"]:has-text("Sign in"):visible, '
                'button[type="submit"]:has-text("Log in"):visible'
            ).count() > 0
            if still_has_pw_form or still_has_signin_button:
                log.warning(f"[{ctx['org_key']}] Login failed — password form still on page (url={page.url})")
                ctx['logged_in'] = False
                return False
            log.info(f"[{ctx['org_key']}] Login successful! (page state: form gone, url={page.url})")
            ctx['logged_in'] = True
            self.save_session(ctx)
            return True
        except Exception as e:
            log.error(f"[{ctx['org_key']}] Login error: {e}")
            return False

    def checkout(self, org_key, org_config, truck_name, truck_url, force_login=False):
        start_time = time.time()
        log.info(f"[{org_key}] === CHECKOUT: {truck_name} ===")
        # Cross-process lock: serialise against the monitor/script path so
        # we never double-charge on a truck. Acquired BEFORE we touch the
        # persistent browser so a contended checkout doesn't burn page time.
        with exclusive_purchase_lock(truck_url, dedup_within_seconds=60) as (ok, reason):
            if not ok:
                log.info(f"[{org_key}] ⏭️  skipping {truck_name}: {reason}")
                return 'SKIPPED', f"locked or recently attempted: {reason}", time.time() - start_time
            return self._checkout_inner(org_key, org_config, truck_name, truck_url, start_time, force_login=force_login)

    def _checkout_inner(self, org_key, org_config, truck_name, truck_url, start_time, force_login=False):
        # Activate per-checkout capture so the listeners on the persistent
        # page write into a fresh buffer. Always finalize in the finally so
        # success and failure both leave a JSON artifact on disk.
        _capture_reset(org_key, truck_name, truck_url)
        status, msg = 'FAILED', 'unknown'
        page = None
        try:
            ctx = self.get_or_create_context(org_key)
            page = ctx['page']
            # Login. `force_login` skips the liveness probe and drives
            # the sign-in form unconditionally — used by the per-customer
            # Test Buy where the caller has already wiped cookies and
            # explicitly wants a fresh authenticated session.
            _capture_step("login")
            if not self.ensure_logged_in(ctx, org_config, force=force_login):
                status, msg = 'FAILED', 'Login failed'
                return status, msg, time.time() - start_time
            # Navigate to product
            _capture_step("navigate", {"truck_url": truck_url})
            page.goto(truck_url, wait_until='domcontentloaded', timeout=15000)
            time.sleep(0.5)

            # Login-wall check. `ensure_logged_in` runs a generic probe
            # but Good360 can still expire/scope the cookie such that
            # a specific catalog URL serves a "you must first login"
            # stub. Detect that on the truck page itself and force a
            # fresh login before giving up — this is the failure mode
            # the AI diagnosis flagged for the MV Softlines URL.
            if _hit_login_wall(page):
                log.warning(f"[{org_key}] login wall on {truck_url} — refreshing session and retrying")
                _capture_step("login_wall_detected", {"url": page.url})
                ctx['logged_in'] = False
                if not self.ensure_logged_in(ctx, org_config):
                    status, msg = 'FAILED', 'Session refresh failed (credentials invalid or account locked?)'
                    return status, msg, time.time() - start_time
                page.goto(truck_url, wait_until='domcontentloaded', timeout=15000)
                time.sleep(0.5)
                if _hit_login_wall(page):
                    _capture_step("login_wall_persists")
                    status, msg = 'FAILED', 'Still hit login wall after re-login — this account may not have catalog access'
                    return status, msg, time.time() - start_time

            if 'Not available' in page.inner_text('body'):
                status, msg = 'MISSED', 'Truck sold out'
                return status, msg, time.time() - start_time
            # Some truck URLs are listing/placeholder pages — the actual
            # Add-to-Cart only renders after clicking a product card.
            _capture_step("ensure_product_detail")
            ok, reason = _ensure_on_product_detail_daemon(page, truck_name)
            if not ok:
                # Operator-actionable per-reason messages so the AI
                # diagnosis path can suggest the right next step.
                _per_reason = {
                    "url_redirects_to_home":
                        "Good360 redirected the URL to /marketplace/ home — this placeholder URL doesn't resolve to a product. Paste the real product URL.",
                    "no_product_links_on_page":
                        "Landing page has no product links — likely a wrong / outdated URL or a logged-out wall the script didn't detect.",
                    "no_product_links_match_name":
                        f"Page has products but none match {truck_name!r} by name — the URL points to a different category.",
                    "no_add_to_cart_after_drill":
                        "Clicked a product card but the detail page never rendered an Add-to-Cart — the truck may be sold out.",
                    "no_add_to_cart_after_text_match":
                        "Clicked the matched product but Add-to-Cart did not appear — sold out or a different account is required.",
                    "structured_click_failed":
                        "Found a product card but clicking it failed — selector instability.",
                    "fallback_click_failed":
                        "Found a name-match anchor but clicking it failed — selector instability.",
                }
                msg = _per_reason.get(reason, f"Add to Cart not reachable: {reason}")
                status = 'FAILED'
                return status, msg, time.time() - start_time

            # Add to cart
            _capture_step("add_to_cart")
            add_btn = page.locator('button:has-text("Add to Cart"), a:has-text("Add to Cart"), [class*="add-to-cart"]').first
            if not add_btn.is_visible(timeout=3000):
                status, msg = 'FAILED', 'Add to Cart not found'
                return status, msg, time.time() - start_time
            add_btn.click()

            # Truck Quote Request modal handling. Trucks DON'T go through
            # the normal cart/checkout flow — Good360 pops a react-confirm
            # overlay saying "Full truckload can not be mixed with general
            # products. Press Continue to empty cart and get a truckload
            # quote." Without dismissing this dialog, every subsequent
            # click hits the overlay and silently fails (this was the
            # actual cause of all the "Place Order button not found" and
            # "No Checkout button" failures).
            _capture_step("post_add_modal_check")
            # The truck-quote modal only appears when the cart had other
            # items before this add. On a fresh cart (which force_login
            # always gives us) the modal is skipped and the mini-cart
            # sidebar opens directly. Don't use body-text fallbacks —
            # "Truck Quote Request" appears as descriptive copy on the
            # product page itself, so a text check false-positives.
            continue_btn_sel = (
                'div.react-confirm-alert-body button:has-text("Continue"), '
                '#react-confirm-alert button:has-text("Continue")'
            )
            modal_visible = False
            try:
                page.wait_for_selector(continue_btn_sel, state='visible', timeout=4000)
                modal_visible = True
                log.info(f"[{ctx['org_key']}] Truck Quote modal detected (Continue button visible)")
            except Exception:
                log.info(f"[{ctx['org_key']}] no modal — cart was empty so the truck added directly")
            if modal_visible:
                # Click "Continue" to clear cart and add the truck.
                _capture_step("click_modal_continue")
                page.locator(continue_btn_sel).first.click()
                log.info(f"[{org_key}] clicked Continue on Truck Quote modal")
                # Wait for the overlay to detach so subsequent clicks
                # don't get intercepted by the dismissing modal.
                try:
                    page.wait_for_selector(
                        'div.react-confirm-alert-overlay',
                        state='detached',
                        timeout=8000,
                    )
                except Exception:
                    pass
                time.sleep(2)

            # After Continue, Good360 opens a slide-in mini-cart on the
            # right with the truck added and a "Checkout" button. There
            # is NO standalone cart URL — /marketplace/cart 404s. The
            # entire cart UI is this sidebar inside `<aside class="miniCart-*">`.
            _capture_step("await_minicart")
            try:
                page.wait_for_selector(
                    'aside[class*="miniCart"]',
                    state='visible',
                    timeout=10000,
                )
            except Exception:
                pass
            minicart = page.locator('aside[class*="miniCart"]').first
            if not minicart.count() or not minicart.is_visible(timeout=1500):
                status, msg = 'FAILED', 'Mini-cart sidebar did not open after Add-to-Cart'
                return status, msg, time.time() - start_time

            log.info(f"[{org_key}] mini-cart open — clicking Checkout inside it")
            _capture_step("click_minicart_checkout")
            mc_checkout = minicart.locator('button:has-text("Checkout")').first
            if not mc_checkout.is_visible(timeout=3000):
                status, msg = 'FAILED', 'No Checkout button inside the mini-cart sidebar'
                return status, msg, time.time() - start_time
            try:
                mc_checkout.click()
                page.wait_for_load_state('domcontentloaded', timeout=15000)
            except Exception as e:
                status, msg = 'FAILED', f'Mini-cart Checkout click failed: {e}'
                return status, msg, time.time() - start_time

            # Wait for the actual checkout page form to mount.
            try:
                page.wait_for_selector(
                    'textarea:visible, '
                    'input[type="text"]:visible, '
                    'button:has-text("Submit"):visible, '
                    'button:has-text("Place Order"):visible, '
                    'button:has-text("Request"):visible',
                    timeout=15000,
                )
            except Exception:
                pass

            cur_url = (page.url or "").lower()
            log.info(f"[{org_key}] post-Checkout landing page: {cur_url}")

            # Good360 checkout is a 3-step wizard:
            #   1. Shipping address  →  "Continue to checkout"
            #   2. Checkout questions →  "Continue to payment"
            #   3. Payment method    →  "Place order"
            # Each step has its own submit button; you can't skip ahead.
            # Customer accounts have saved addresses + cards, so steps 1
            # and 3 are usually just "click Continue / Place order".

            # === Step 1 → 2: Shipping address → "Continue to checkout" ===
            _capture_step("continue_to_checkout")
            try:
                page.wait_for_selector(
                    'button:has-text("Continue to checkout"):visible',
                    timeout=10000,
                )
                page.locator('button:has-text("Continue to checkout"):visible').first.click()
                log.info(f"[{org_key}] clicked 'Continue to checkout' (step 1→2)")
                time.sleep(3)
            except Exception as e:
                log.warning(f"[{org_key}] no 'Continue to checkout' button: {e}")

            # === Step 2: Fill questions → "Continue to payment" ===
            #
            # Good360's checkout questions are textareas with no
            # placeholder/name/aria-label attributes — the question
            # text lives in a separate <label> above each field, so
            # attribute matching finds nothing and the fields stay
            # empty. The questions arrive in a known order though:
            #   1. Number of recipients (numerical)
            #   2. Distribution method (free text)
            #   3. Retype delivery address (free text)
            #   4. Dock/pallet capability (<select> with options)
            # Position-based fill handles this reliably regardless of
            # attribute presence.
            _capture_step("fill_questions")
            answers = org_config.get('checkout_answers', {})
            # Step 2's question form renders progressively. Wait for the
            # checkout-questions section to actually mount before reading
            # fields. The section header text "Checkout questions" is the
            # most reliable anchor — wait for it AND for ≥2 textareas to
            # exist before filling.
            try:
                page.wait_for_function(
                    """() => {
                        const sections = document.querySelectorAll('section, [class*="step"], [class*="panel"]');
                        const hasQuestionsSection = Array.from(sections).some(s =>
                            (s.innerText || '').toLowerCase().includes('number of recipients'));
                        const visibleTextareas = Array.from(document.querySelectorAll('textarea'))
                            .filter(t => t.offsetParent !== null).length;
                        return hasQuestionsSection || visibleTextareas >= 2;
                    }""",
                    timeout=12000,
                )
            except Exception:
                pass
            ordered_answers = [
                answers.get('people_helped', '300'),
                answers.get('distribution_method',
                            'Distributed through community outreach to families in need'),
                answers.get('warehouse_address',
                            '1025 Progress Circle Lawrenceville, GA 30043'),
            ]
            # Good360 names question fields `restriction[question-NNN]`.
            # That selector is way more precise than "any visible textarea"
            # — the latter picked up unrelated fields (address inputs,
            # quantity boxes) and clobbered them with our checkout answers.
            #
            # Critical: Good360's form is React-controlled. Playwright's
            # .fill() sets the DOM .value but React internally tracks
            # values through the native HTMLInputElement.value setter on
            # the prototype — bypassing that setter means React's state
            # doesn't update, the field re-renders empty on the next
            # render, and the form sees the field as empty when
            # validating. The fix is to call the prototype's value setter
            # directly and then dispatch 'input' so React's onChange runs.
            _REACT_FILL_JS = """
                (el, value) => {
                    const proto = el.tagName === 'TEXTAREA'
                        ? window.HTMLTextAreaElement.prototype
                        : window.HTMLInputElement.prototype;
                    const setter = Object.getOwnPropertyDescriptor(proto, 'value').set;
                    setter.call(el, value);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                }
            """
            try:
                question_fields = page.locator('[name^="restriction[question-"]:visible').all()
                log.info(f"[{org_key}] step-2 sees {len(question_fields)} restriction[question-*] fields")
                for i, f in enumerate(question_fields):
                    nm = f.get_attribute('name') or ''
                    tag = f.evaluate('el => el.tagName.toLowerCase()')
                    log.info(f"[{org_key}]   [{i}] <{tag} name={nm!r}>")
                # Map answers to fields positionally. Good360 orders questions
                # as: recipients → distribution → retype-address → (select).
                # We fill text/textarea ones with our ordered_answers and
                # leave the <select> for the dropdown block below.
                text_q = [f for f in question_fields
                          if f.evaluate('el => el.tagName.toLowerCase()') in ('input', 'textarea')]
                for idx, value in enumerate(ordered_answers):
                    if idx >= len(text_q):
                        break
                    try:
                        text_q[idx].evaluate(_REACT_FILL_JS, value)
                        log.info(f"[{org_key}]   filled question[{idx}] with {value[:40]!r}")
                    except Exception as e:
                        log.warning(f"[{org_key}] fill question {idx} failed: {e}")
            except Exception as e:
                log.warning(f"[{org_key}] fill_questions error: {e}")

            # Dock/pallet dropdown. Good360 uses **react-select** (not
            # native <select>), so `select[name=...]` never matches.
            # The dropdown renders as a `.select__control` with a
            # `.select__placeholder` reading "Select…" until something
            # is chosen. We find the unselected control and click it
            # open, then click an option matching dock/pallet/forklift.
            def _pick_react_select(control_loc, keywords, log_label):
                """Open a react-select control and click the first option
                whose visible text contains any of `keywords`. Falls
                back to the first non-placeholder option."""
                try:
                    control_loc.scroll_into_view_if_needed(timeout=3000)
                    control_loc.click(timeout=5000)
                    time.sleep(0.6)
                    opts = page.locator('div[class*="select__option"]:visible').all()
                    if not opts:
                        log.warning(f"[{org_key}] {log_label}: no options visible")
                        return False
                    chosen = None
                    chosen_text = ''
                    for opt in opts:
                        try:
                            t = (opt.inner_text() or '').strip()
                            tl = t.lower()
                            if any(k in tl for k in keywords):
                                chosen, chosen_text = opt, t
                                break
                        except Exception:
                            continue
                    if chosen is None:
                        # No keyword match — pick first option
                        chosen, chosen_text = opts[0], (opts[0].inner_text() or '').strip()
                    chosen.click()
                    time.sleep(0.4)
                    log.info(f"[{org_key}] {log_label} → {chosen_text[:60]!r}")
                    return True
                except Exception as e:
                    log.warning(f"[{org_key}] {log_label} failed: {e}")
                    # Close the menu if it stuck open
                    try: page.keyboard.press('Escape')
                    except: pass
                    return False

            try:
                # Find all react-select controls currently showing a
                # placeholder (= unselected). On step 2 the shipping
                # address dropdown is already filled, so it won't have
                # `.select__placeholder` visible — only the dock/pallet
                # question will.
                unfilled = page.locator(
                    'div[class*="select__control"]:visible:has(div[class*="select__placeholder"])'
                ).all()
                log.info(f"[{org_key}] step-2 sees {len(unfilled)} unfilled react-select widgets")
                # We only need the dock/pallet dropdown. Stop after the
                # first successful pick — the remaining controls are
                # off-screen or unrelated (shipping carrier, etc.) and
                # each failed attempt wastes 15s waiting on scroll/click.
                for ctl in unfilled:
                    if _pick_react_select(
                        ctl,
                        keywords=('loading dock', 'dock', 'pallet jack', 'forklift', 'yes'),
                        log_label='dock/pallet dropdown',
                    ):
                        break
            except Exception as e:
                log.warning(f"[{org_key}] react-select scan error: {e}")

            # Legacy: native <select> fallback (in case Good360 changes
            # back to a real select later). No-op for current site.
            try:
                for sel in page.locator('select[name^="restriction[question-"]:visible').all():
                    sel_name = sel.get_attribute('name') or ''
                    options = sel.locator('option').all()
                    chosen_val = None
                    chosen_label = None
                    for opt in options[1:]:  # skip placeholder
                        txt = (opt.inner_text() or '').lower()
                        if any(k in txt for k in ('dock', 'pallet', 'yes', 'forklift')):
                            chosen_val = opt.get_attribute('value')
                            chosen_label = opt.inner_text()
                            break
                    if chosen_val is None and len(options) > 1:
                        # No keyword match — pick first non-placeholder
                        # option so we at least satisfy the required field.
                        chosen_val = options[1].get_attribute('value')
                        chosen_label = options[1].inner_text()
                    if chosen_val is not None:
                        # React-aware select: native setter + change event.
                        try:
                            sel.evaluate(
                                """(el, value) => {
                                    const setter = Object.getOwnPropertyDescriptor(
                                        window.HTMLSelectElement.prototype, 'value').set;
                                    setter.call(el, value);
                                    el.dispatchEvent(new Event('change', {bubbles: true}));
                                    el.dispatchEvent(new Event('blur', {bubbles: true}));
                                }""",
                                chosen_val,
                            )
                        except Exception:
                            try:
                                sel.select_option(value=chosen_val)
                            except Exception:
                                try:
                                    sel.select_option(label=chosen_label)
                                except Exception:
                                    pass
                        log.info(f"[{org_key}] selected {sel_name} → {chosen_label!r}")
            except Exception as e:
                log.warning(f"[{org_key}] dropdown selection error: {e}")

            _capture_step("continue_to_payment")
            try:
                page.wait_for_selector(
                    'button:has-text("Continue to payment"):visible',
                    timeout=10000,
                )
                page.locator('button:has-text("Continue to payment"):visible').first.click()
                log.info(f"[{org_key}] clicked 'Continue to payment' (step 2→3)")
                time.sleep(3)
            except Exception as e:
                log.warning(f"[{org_key}] no 'Continue to payment' button: {e}")

            # === Step 3 prep: Billing address ===
            # Good360 collapses the Payment-method section by default
            # after step 2; clicks on its dropdowns get intercepted by
            # the `.steps-collapseHead` element until we expand it.
            # Click the section header to expand step 3 first.
            _capture_step("expand_step3")
            try:
                # The "Payment method" step header is clickable. Match
                # by text — the css-modules hash on the wrapper class
                # rotates, so text-matching is more stable.
                step3_head = page.locator(
                    'div[class*="collapseHead"]:has-text("Payment method"), '
                    'h3:has-text("Payment method"), '
                    'div:has-text("Add billing information")'
                ).first
                if step3_head.count() and step3_head.is_visible(timeout=2000):
                    try:
                        step3_head.click(timeout=4000)
                        time.sleep(1)
                        log.info(f"[{org_key}] expanded step-3 (Payment method)")
                    except Exception as e:
                        log.warning(f"[{org_key}] step-3 expand click failed: {e}")
            except Exception as e:
                log.warning(f"[{org_key}] step-3 expand error: {e}")

            # Step 3 has SIX react-select dropdowns. Picking the saved
            # billing address (matching by zip) auto-populates the
            # billingAddress inputs + state + country in one shot, so
            # we don't fill those inputs individually.
            # Required order:
            #   1. Saved billing address (zip match)
            #   2. Card type (Visa/MC/Amex/Discover)
            #   3. Expiry month + Expiry year
            #   4. Card number + CVV (inline inputs:
            #      `cyberSource.cardNumber` + `cyberSource.securityCode`)
            # The 6th dropdown (Country) is disabled — pre-set to US.

            def _react_set(loc, val):
                """React-aware value set: native setter + bubbling events."""
                loc.evaluate(
                    """(el, value) => {
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
                    }""",
                    val,
                )

            def _pick_by_label(label_substr, value_match, log_label):
                """Find an unfilled react-select whose nearby label text
                contains `label_substr`, open it, and click an option
                whose visible text contains `value_match`."""
                unfilled = page.locator(
                    'div[class*="select__control"]:visible:has(div[class*="select__placeholder"])'
                ).all()
                target_ctl = None
                for ctl in unfilled:
                    try:
                        ctx_text = ctl.evaluate(
                            """el => {
                                let n = el;
                                for (let i=0; i<6 && n; i++) {
                                    n = n.parentElement; if(!n) break;
                                    const tx = (n.innerText || '').trim();
                                    if (tx && tx.length < 200) {
                                        const lines = tx.split('\\n')
                                            .map(s=>s.trim())
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
                        target_ctl = ctl
                        break
                if target_ctl is None:
                    log.warning(f"[{org_key}] {log_label}: no dropdown labeled "
                                f"~{label_substr!r}")
                    return False
                try:
                    target_ctl.scroll_into_view_if_needed(timeout=3000)
                    target_ctl.click(timeout=5000, force=True)
                    time.sleep(0.7)
                    opts = page.locator('div[class*="select__option"]:visible').all()
                    for o in opts:
                        try:
                            t = (o.inner_text() or '').strip()
                            if value_match.lower() in t.lower():
                                o.click()
                                time.sleep(0.6)
                                log.info(f"[{org_key}] {log_label} → {t[:70]!r}")
                                return True
                        except Exception:
                            continue
                    page.keyboard.press('Escape')
                    log.warning(f"[{org_key}] {log_label}: no option matched "
                                f"~{value_match!r} ({len(opts)} options)")
                    return False
                except Exception as e:
                    log.warning(f"[{org_key}] {log_label} click error: {e}")
                    try: page.keyboard.press('Escape')
                    except: pass
                    return False

            # Per [[feedback-customer-data-only]] in MEMORY.md, all
            # billing/card data must come from the customer record.
            # mission-control populates org_config['billing_address']
            # from the primary card's billing_address; we use the zip
            # to match a saved address on the Good360 account side.
            ba   = dict(org_config.get('billing_address') or {})
            card = org_config.get('card') or {}
            if not ba.get('postcode'):
                msg = ("billing_address.postcode is required to match a "
                       "saved address on the Good360 account.")
                log.error(f"[{org_key}] {msg}")
                return 'FAILED', msg, time.time() - start_time

            # 1. Saved billing address — match by zip first, street if
            # zip didn't hit. The pick auto-populates state + country.
            _capture_step("pick_billing_address")
            picked_addr = _pick_by_label('billing address', ba['postcode'],
                                          'billing.address')
            if not picked_addr and ba.get('street'):
                street_token = ba['street'].split()[0]
                picked_addr = _pick_by_label('billing address', street_token,
                                              'billing.address')
            time.sleep(1)

            # 2. Card type. Good360 options are Visa / Mastercard /
            # Amex / Discover. card.type from sandbox.card_for_org() is
            # lowercase ('visa'); capitalize for the substring match.
            _capture_step("pick_card_type")
            card_type = (card.get('type') or '').strip()
            if card_type:
                _pick_by_label('card type', card_type, 'card.type')

            # 3. Expiry — split MMYY into month / 4-digit year.
            exp = (card.get('expiry') or '').strip().zfill(4)
            exp_m, exp_y = (exp[:2], '20' + exp[2:4]) if len(exp) == 4 else ('', '')
            if exp_m:
                _capture_step("pick_exp_month")
                _pick_by_label('expiry month', exp_m, 'card.exp_month')
            if exp_y:
                _capture_step("pick_exp_year")
                _pick_by_label('expiry year', exp_y, 'card.exp_year')

            # 4. Payment-method radio (chcybersource). Often
            # auto-selected; the check is idempotent.
            _capture_step("select_payment_method")
            try:
                for r in page.locator('input[type="radio"]:visible').all():
                    val = (r.get_attribute('value') or '').lower()
                    if any(k in val for k in ('card', 'cyber', 'visa', 'credit')):
                        try:
                            r.check()
                            log.info(f"[{org_key}] payment-method radio: {val}")
                            break
                        except Exception:
                            pass
            except Exception as e:
                log.warning(f"[{org_key}] payment-method radio error: {e}")

            # 5. Card number + CVV. Good360 names them
            # `cyberSource.cardNumber` and `cyberSource.securityCode`
            # (inline inputs, no iframe — they tokenize client-side via
            # flex.cybersource.com before Place Order POSTs the token).
            _capture_step("fill_card_fields")
            try:
                cn = page.locator('input[name="cyberSource.cardNumber"]:visible')
                if cn.count() and card.get('number'):
                    _react_set(cn.first, card['number'])
                    log.info(f"[{org_key}] cyberSource.cardNumber filled "
                             f"(****{card['number'][-4:]})")
                cv = page.locator('input[name="cyberSource.securityCode"]:visible')
                if cv.count() and card.get('cvv'):
                    _react_set(cv.first, card['cvv'])
                    log.info(f"[{org_key}] cyberSource.securityCode filled")
            except Exception as e:
                log.warning(f"[{org_key}] cyberSource field fill failed: {e}")
            finally:
                # Scrub card secrets from org_config so they don't
                # linger in subsequent capture writes.
                if isinstance(card, dict):
                    for _k in ('number', 'expiry', 'cvv'):
                        card.pop(_k, None)

            # === Place order ===
            os.makedirs(SCREENSHOT_DIR, exist_ok=True)
            shot_prefix = f"{SCREENSHOT_DIR}/{int(time.time())}_{org_key}"
            _capture_step("place_order")
            # Wait for the button to be visible AND enabled. The wizard
            # toggles `disabled` once the payment radio is selected and
            # the billing address is locked in — a plain `:visible` wait
            # would fire too early and the click would hang on the
            # "waiting for element to be enabled" retry loop.
            place_btn_sel = (
                'button:has-text("Place order"):not([disabled]), '
                'button:has-text("Place Order"):not([disabled])'
            )
            try:
                page.wait_for_selector(place_btn_sel, state='visible', timeout=15000)
            except Exception:
                log.warning(f"[{org_key}] Place Order didn't enable in 15s — trying anyway")
            place_btn = page.locator(
                'button:has-text("Place order"), button:has-text("Place Order")'
            ).first
            if not place_btn.is_visible(timeout=3000):
                status, msg = 'FAILED', 'Place Order button not found after wizard'
                return status, msg, time.time() - start_time
            try:
                place_btn.click(timeout=15000)
                time.sleep(4)
            except Exception as e:
                # Capture a screenshot of the disabled state so the
                # operator can see what was missing on the page.
                try:
                    page.screenshot(path=f"{shot_prefix}_place_order_disabled.png", full_page=True)
                except Exception:
                    pass
                status, msg = 'FAILED', f'Place Order click failed (likely disabled — required field missing): {e}'
                return status, msg, time.time() - start_time
            page.screenshot(path=f"{shot_prefix}_post_order.png")
            # Tighter success heuristic. The old check matched the bare
            # word "confirmation" anywhere in body text — which hits
            # benign UI text and produced false positives. Real success
            # is signalled by URL change OR an order-number pattern OR
            # an explicit thank-you phrase. Card-declined is its own
            # tier — it means the form really submitted to the
            # processor (a strong end-to-end signal in sandbox tests).
            time.sleep(2)  # let any redirect settle
            cur_url = page.url
            text   = page.inner_text('body')
            text_l = text.lower()

            url_success = (
                '/onepage/success' in cur_url
                or '/checkout/success' in cur_url
                or '/order/' in cur_url
                or 'success' in cur_url.split('/')[-1].lower()
            )
            import re as _re
            order_num_match = _re.search(
                r'order\s*#\s*[\w-]+|order\s+number[:#\s]+[\w-]+',
                text_l,
            )
            thanks = any(p in text_l for p in (
                'thank you for your order',
                'order has been placed',
                'order was placed successfully',
                'your order is confirmed',
                'order confirmed',
            ))
            # Order matters: the most specific phrases first so the
            # summary lands on the real error banner, not a field label
            # that incidentally contains the same word.
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

            if url_success or order_num_match or thanks:
                detail = []
                if url_success: detail.append(f"url={cur_url}")
                if order_num_match: detail.append(f"#={order_num_match.group(0).strip()}")
                if thanks: detail.append("thank-you-message")
                return 'SUCCESS', f"Order placed ({', '.join(detail)})", time.time() - start_time

            for bad in decline_markers:
                if bad in text_l:
                    return ('CARD_DECLINED',
                            f"Payment processor rejected card: {bad!r}",
                            time.time() - start_time)

            for sold in ('not available', 'sold out'):
                if sold in text_l:
                    return 'MISSED', f"Sold out during checkout ({sold!r})", time.time() - start_time

            return ('MANUAL',
                    f'No success or decline indicator (url={cur_url}) — check screenshot',
                    time.time() - start_time)
        except Exception as e:
            log.error(f"[{org_key}] Error: {e}")
            status, msg = 'FAILED', str(e)
            return status, msg, time.time() - start_time
        finally:
            _capture_finalize(page, status, msg)

    # ============================================================
    # LIVE VIEW (operator-driven, separate from autobuy contexts)
    # ============================================================
    # The Live View page in the dashboard uses a dedicated browser context
    # so it doesn't share session state with the autobuy contexts. Three
    # entry points: navigate, prepare_checkout (login + cart + fill, stops
    # before Place Order), and place_order (commits the prepared form).

    LIVE_KEY = "_live_view"

    def live_ctx(self):
        # Reuse get_or_create_context — the LIVE_KEY context gets its own
        # storage_state file under browser_data/_live_view/, kept separate
        # from per-org sessions.
        return self.get_or_create_context(self.LIVE_KEY)

    def live_screenshot(self) -> bytes:
        """Return the current Live View viewport as PNG bytes."""
        ctx = self.live_ctx()
        return ctx['page'].screenshot(type='png', full_page=False)

    def live_navigate(self, url: str):
        """Drive the Live View context to the given URL."""
        ctx = self.live_ctx()
        ctx['page'].goto(url, wait_until='domcontentloaded', timeout=20000)
        return True

    def live_prepare_checkout(self, org_key: str, truck_url: str):
        """Open the truck, add to cart, fill the checkout form for `org_key`,
        and stop just before the Place Order click. Operator reviews the
        screenshots and decides whether to commit via live_place_order().
        """
        all_orgs = _cfg.load_orgs()
        org_config = all_orgs.get(org_key) or {}

        log.info(f"[live] preparing checkout for org={org_key} url={truck_url}")
        ctx = self.live_ctx()
        page = ctx['page']

        # Login credentials precedence (live mode):
        #   1. sandbox.org_credentials() — swaps to SANDBOX_GOOD360_* in
        #      sandbox mode, otherwise passes through org_config creds.
        #   2. SCAN_GOOD360_* env vars — the master account the monitor
        #      uses to scan listings. ALWAYS works for live mode because
        #      the legacy `good360_orgs_master.example.json` only ships
        #      placeholder credentials and the org_config path lies.
        # Live View + Fetch Price both end up here, so this is the one
        # place we resolve which account drives the daemon's browser.
        sbx_email, sbx_password = sandbox.org_credentials(
            org_config.get('good360_email', ''), org_config.get('good360_password', ''),
        )
        scan_email = os.environ.get('SCAN_GOOD360_EMAIL', '')
        scan_password = os.environ.get('SCAN_GOOD360_PASSWORD', '')
        live_org_config = dict(org_config)
        live_org_config['good360_email']    = sbx_email or scan_email
        live_org_config['good360_password'] = sbx_password or scan_password

        if not live_org_config['good360_email'] or not live_org_config['good360_password']:
            return 'FAILED', 'no usable Good360 credentials (set SCAN_GOOD360_EMAIL / SCAN_GOOD360_PASSWORD)'

        log.info(f"[live] using login account: {live_org_config['good360_email']!r}")
        if not self.ensure_logged_in(ctx, live_org_config):
            return 'FAILED', f"Login failed as {live_org_config['good360_email']}"

        try:
            page.goto(truck_url, wait_until='domcontentloaded', timeout=20000)
            time.sleep(0.5)
            if 'Not available' in page.inner_text('body'):
                return 'MISSED', 'Truck not available'

            # Add to cart
            add_btn = page.locator('button:has-text("Add to Cart"), a:has-text("Add to Cart"), [class*="add-to-cart"]').first
            if not add_btn.is_visible(timeout=3000):
                return 'FAILED', 'Add to Cart not found'
            add_btn.click()
            time.sleep(1)

            # Continue to checkout
            checkout_btn = page.locator('button:has-text("Checkout"), button:has-text("Proceed")').first
            if checkout_btn.is_visible(timeout=3000):
                checkout_btn.click()
                time.sleep(1.5)

            # Fill questions (best-effort placeholder matching)
            answers = org_config.get('checkout_answers', {})
            try:
                for field in page.locator('textarea, input[type="text"]').all()[:4]:
                    ph = (field.get_attribute('placeholder') or '').lower()
                    if 'people' in ph or 'help' in ph:
                        field.fill(answers.get('people_helped', '300'))
                    elif 'distribut' in ph or 'how' in ph:
                        field.fill(answers.get('distribution_method', 'Homeless'))
            except Exception: pass

            # Dropdowns
            try:
                for sel in page.locator('select').all():
                    for opt in sel.locator('option').all()[1:]:
                        if 'dock' in opt.inner_text().lower() or 'pallet' in opt.inner_text().lower():
                            sel.select_option(index=sel.locator('option').all().index(opt))
                            break
            except Exception: pass

            # Card (sandbox helper swaps to test card automatically)
            card = sandbox.card_for_org(org_config.get('card')) or org_config.get('card') or {}
            try:
                for inp in page.locator('input').all()[:20]:
                    n = (inp.get_attribute('name') or '').lower()
                    if 'number' in n: inp.fill(card.get('number', ''))
                    elif 'exp' in n: inp.fill(card.get('expiry', ''))
                    elif 'cvv' in n or 'cvc' in n: inp.fill(card.get('cvv', ''))
            except Exception: pass

            log.info(f"[live] prepared {org_key} @ {truck_url}")
            return 'READY', 'Checkout filled — review then click Place Order'
        except Exception as e:
            log.error(f"[live] prepare error: {e}")
            return 'FAILED', str(e)

    def live_fetch_price(self, org_key: str, truck_url: str):
        """Drive the daemon's live browser through cart/checkout just far
        enough to surface the admin fee, then read it from the page.

        Good360 hides prices on the listing for the scan account but the
        cart + checkout pages render the real number. We reuse
        live_prepare_checkout (which gets us to the price-visible state)
        and regex the largest $-amount out of the rendered body.

        Side effect: leaves the truck in the operator's cart on Good360.
        We don't bother clearing it — the next prepare_checkout will
        overwrite the cart state, and an item in cart costs nothing.
        """
        # If the truck isn't AVAILABLE, prepare_checkout will return MISSED
        # because Add-to-Cart won't render. That's a clean failure mode.
        status, msg = self.live_prepare_checkout(org_key, truck_url)
        if status != 'READY':
            return None, status, msg
        page = self.live_ctx()['page']
        try:
            body = page.inner_text('body')
        except Exception as e:
            return None, 'FAILED', f'page read error: {e}'
        # Two-decimal $-amounts are admin fees / totals. One-decimal or
        # zero-decimal could be unrelated copy ("under $5K trucks").
        import re as _re
        amounts = _re.findall(r'\$\s*([\d,]+\.\d{2})', body)
        if not amounts:
            return None, 'FAILED', 'no dollar amounts on page after prepare_checkout'
        try:
            # Largest = the truck's total (admin + shipping).
            price = max(float(a.replace(',', '')) for a in amounts)
            return price, 'OK', f'extracted ${price:.2f} from {len(amounts)} candidate amounts'
        except ValueError:
            return None, 'FAILED', 'amount parse error'

    def live_place_order(self):
        """Commit the prepared form. Caller must have run live_prepare_checkout
        first; otherwise we have nothing meaningful to click."""
        ctx = self.live_ctx()
        page = ctx['page']
        try:
            place_btn = page.locator(
                'button:has-text("Place Order"), button:has-text("Place order"), '
                'button:has-text("Complete"), button[type="submit"]'
            ).first
            if not place_btn.is_visible(timeout=3000):
                return 'FAILED', 'Place Order button not found — was prepare_checkout run?'
            place_btn.click()
            # Poll for a confirmation indicator up to 30s.
            try:
                page.wait_for_function(
                    """() => {
                        const t = ((document.body && document.body.innerText) || '').toLowerCase();
                        return /thank you|order confirmed|order number|order\\s*#|payment successful/.test(t);
                    }""",
                    timeout=30000,
                )
            except Exception:
                pass
            text = page.inner_text('body').lower()
            for ok in ['thank you', 'order confirmed', 'confirmation', 'order #']:
                if ok in text:
                    return 'SUCCESS', f'Order placed (matched {ok!r})'
            for bad in ['not available', 'sold out']:
                if bad in text:
                    return 'MISSED', 'Sold out during commit'
            return 'MANUAL', 'No confirmation indicator — review the page'
        except Exception as e:
            log.error(f"[live] place_order error: {e}")
            return 'FAILED', str(e)

    def shutdown(self):
        self.running = False
        for k, ctx in list(self.contexts.items()):
            try: ctx['context'].close()
            except: pass
        if self.browser: self.browser.close()
        if self.playwright: self.playwright.stop()

manager = BrowserManager()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args): log.info(f"HTTP: {args[0]}")

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == '/health':
            return self._json(200, {'status':'ok','contexts':list(manager.contexts.keys())})
        if self.path.startswith('/live/screenshot'):
            try:
                png = manager.live_screenshot()
            except Exception as e:
                return self._json(500, {'status':'error','message':str(e)})
            self.send_response(200)
            self.send_header('Content-Type', 'image/png')
            self.send_header('Content-Length', str(len(png)))
            # Disable any intermediate caching — frontend polls constantly.
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(png)
            return
        self.send_response(404); self.end_headers()

    def do_POST(self):
        # Top-level guard so a JSON-decode error or an unexpected exception
        # in any handler can't crash the request → hang the caller. Every
        # exit path returns a structured JSON body the dashboard / monitor
        # can parse, even on failure.
        try:
            return self._dispatch_post()
        except Exception as _post_err:
            log.exception(f"do_POST({self.path}) unhandled: {_post_err}")
            try:
                self._json(500, {
                    'status': 'error',
                    'message': f'daemon internal error: {type(_post_err).__name__}: {_post_err}',
                })
            except Exception:
                # If even error reporting fails, fall through — connection
                # closes and the caller's request times out, which is at
                # least a detectable signal.
                pass

    # Endpoints that can place a real order, or that log into the live site
    # and fill card data. All refused when ENABLE_AUTO_BUY=false (staging /
    # feature environments). Read-only paths (/health, /live/screenshot,
    # /live/navigate) stay available.
    _PURCHASE_PATHS = frozenset({
        '/checkout',
        '/test_checkout',
        '/live/prepare_checkout',
        '/live/place_order',
        '/live/fetch_price',
    })

    def _dispatch_post(self):
        if self.path in self._PURCHASE_PATHS and not feature_flags.auto_buy_enabled():
            return self._json(403, {
                'status': 'BLOCKED',
                'message': ('auto-buy disabled in this environment '
                            '(ENABLE_AUTO_BUY=false)'),
            })
        raw = self.rfile.read(int(self.headers.get('Content-Length', 0)) or 0) or b'{}'
        try:
            body = json.loads(raw)
        except json.JSONDecodeError as _je:
            return self._json(400, {'status': 'error', 'message': f'invalid JSON body: {_je}'})

        if self.path == '/checkout':
            org_key, truck_name, truck_url = body.get('org_key'), body.get('truck_name'), body.get('truck_url')
            try:
                all_orgs = _cfg.load_orgs()
                org_config = all_orgs.get(org_key)
                if not org_config:
                    return self._json(404, {'status':'error','message':f'org {org_key!r} not configured'})
            except Exception:
                return self._json(500, {'status':'error','message':'orgs load failed'})
            status, msg, elapsed = manager.checkout(org_key, org_config, truck_name, truck_url)
            return self._json(200, {
                'status':       status,
                'message':      msg,
                'elapsed':      round(elapsed, 1),
                'capture_path': _DAEMON_CAPTURE.get('path'),
            })

        if self.path == '/test_checkout':
            # Same flow as /checkout but org_config comes inline in the
            # request body — used by the dashboard's per-customer Test
            # Buy modal so we don't need to register a config-file org
            # for every QuickBeed customer.
            #
            # force_login defaults to True for test-purchase: we never
            # want a stale cookie to silently land us on a login-wall
            # version of the truck page. Production /checkout still
            # trusts the cookie cache for speed.
            org_key = (body.get('org_key') or '__test__')[:64]
            org_config = body.get('org_config') or {}
            truck_name = body.get('truck_name')
            truck_url  = body.get('truck_url')
            force_login = bool(body.get('force_login', True))
            if not (truck_name and truck_url):
                return self._json(400, {'status':'error','message':'truck_name + truck_url required'})
            if not org_config.get('good360_email') or not org_config.get('good360_password'):
                return self._json(400, {'status':'error','message':'org_config.good360_email + good360_password required'})
            if force_login:
                # Wipe BOTH the cached BrowserContext AND the persisted
                # storage_state.json. Clearing cookies on the existing
                # context isn't enough because Good360 puts auth tokens
                # in localStorage too, and localStorage.clear() can only
                # run when the page is on the matching origin (the
                # cached context's page is on about:blank, so the clear
                # is a no-op). Deleting the context outright forces
                # get_or_create_context to build a virgin one with no
                # cookies and no localStorage on the next call.
                ctx_dir = f"{WORKDIR}/browser_data/{org_key}"
                storage_file = f"{ctx_dir}/storage_state.json"
                try:
                    if os.path.exists(storage_file):
                        os.remove(storage_file)
                        log.info(f"[{org_key}] force_login: removed {storage_file}")
                except Exception as e:
                    log.warning(f"[{org_key}] force_login: removing storage_state failed: {e}")
                with manager.lock:
                    cached = manager.contexts.pop(org_key, None)
                if cached is not None:
                    try:
                        cached['context'].close()
                        log.info(f"[{org_key}] force_login: closed and dropped cached BrowserContext")
                    except Exception as e:
                        log.warning(f"[{org_key}] force_login: context close failed: {e}")
                log.info(f"[{org_key}] force_login: fresh context will be built; will re-authenticate")
            status, msg, elapsed = manager.checkout(org_key, org_config, truck_name, truck_url, force_login=force_login)
            return self._json(200, {
                'status':       status,
                'message':      msg,
                'elapsed':      round(elapsed, 1),
                'capture_path': _DAEMON_CAPTURE.get('path'),
            })

        if self.path == '/live/navigate':
            url = (body.get('url') or '').strip()
            if not url:
                return self._json(400, {'status':'error','message':'url required'})
            try:
                manager.live_navigate(url)
                return self._json(200, {'status':'ok','url':url})
            except Exception as e:
                return self._json(500, {'status':'error','message':str(e)})

        if self.path == '/live/prepare_checkout':
            org_key = body.get('org_key')
            truck_url = body.get('truck_url')
            if not org_key or not truck_url:
                return self._json(400, {'status':'error','message':'org_key + truck_url required'})
            status, msg = manager.live_prepare_checkout(org_key, truck_url)
            return self._json(200, {'status':status, 'message':msg})

        if self.path == '/live/place_order':
            status, msg = manager.live_place_order()
            return self._json(200, {'status':status, 'message':msg})

        if self.path == '/live/fetch_price':
            org_key = body.get('org_key')
            truck_url = body.get('truck_url')
            if not org_key or not truck_url:
                return self._json(400, {'status':'error','message':'org_key + truck_url required'})
            price, status, msg = manager.live_fetch_price(org_key, truck_url)
            return self._json(200, {'status':status, 'message':msg, 'price':price})

        if self.path == '/shutdown':
            manager.shutdown()
            return self._json(200, {'status':'shutting_down'})

        self.send_response(404); self.end_headers()

def main():
    log.info("Good360 Persistent Browser Daemon v2 starting...")
    manager.start()
    server = HTTPServer(('0.0.0.0', DAEMON_PORT), Handler)
    log.info(f"API on port {DAEMON_PORT} - Ready!")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: manager.shutdown(); server.server_close()

if __name__ == '__main__': main()
