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
import sandbox  # sandbox-mode URL routing

WORKDIR = os.environ.get("WORKDIR", "/a0/usr/workdir")
SCREENSHOT_DIR = f"{WORKDIR}/browser_screenshots"
LOG_FILE = f"{WORKDIR}/good360_daemon.log"
STATE_FILE = f"{WORKDIR}/good360_daemon_state.json"
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

    def ensure_logged_in(self, ctx, org_config):
        if ctx['logged_in']:
            return True
        email = org_config.get('good360_email', '')
        password = org_config.get('good360_password', '')
        page = ctx['page']
        try:
            log.info(f"[{ctx['org_key']}] Logging in as {email}...")
            page.goto(sandbox.good360_browse_url(), wait_until='domcontentloaded', timeout=20000)
            time.sleep(1)
            # Check if already logged in
            if page.locator('input[placeholder*="email" i]').count() == 0 and '$' in page.inner_text('body'):
                log.info(f"[{ctx['org_key']}] Already logged in (cookies)")
                ctx['logged_in'] = True
                return True
            # Click login
            login_btn = page.locator('text="Log in"').first
            if login_btn.is_visible(timeout=3000):
                login_btn.click()
                time.sleep(1.5)
            page.fill('input[placeholder*="email" i]', email)
            page.fill('input[placeholder*="password" i]', password)
            page.click('button:has-text("Sign in")')
            time.sleep(2)
            if '$' in page.inner_text('body'):
                log.info(f"[{ctx['org_key']}] Login successful!")
                ctx['logged_in'] = True
                self.save_session(ctx)
                return True
            log.warning(f"[{ctx['org_key']}] Login failed")
            return False
        except Exception as e:
            log.error(f"[{ctx['org_key']}] Login error: {e}")
            return False

    def checkout(self, org_key, org_config, truck_name, truck_url):
        start_time = time.time()
        log.info(f"[{org_key}] === CHECKOUT: {truck_name} ===")
        try:
            ctx = self.get_or_create_context(org_key)
            page = ctx['page']
            # Login
            if not self.ensure_logged_in(ctx, org_config):
                return 'FAILED', 'Login failed', time.time() - start_time
            # Navigate to product
            page.goto(truck_url, wait_until='domcontentloaded', timeout=15000)
            time.sleep(0.5)
            if 'Not available' in page.inner_text('body'):
                return 'MISSED', 'Truck sold out', time.time() - start_time
            # Add to cart
            add_btn = page.locator('button:has-text("Add to Cart"), a:has-text("Add to Cart"), [class*="add-to-cart"]').first
            if not add_btn.is_visible(timeout=3000):
                return 'FAILED', 'Add to Cart not found', time.time() - start_time
            add_btn.click()
            time.sleep(1)
            # Checkout
            checkout_btn = page.locator('button:has-text("Checkout"), button:has-text("Proceed")').first
            if checkout_btn.is_visible(timeout=3000):
                checkout_btn.click()
                time.sleep(1.5)
            # Fill questions
            answers = org_config.get('checkout_answers', {})
            try:
                for field in page.locator('textarea, input[type="text"]').all()[:4]:
                    ph = (field.get_attribute('placeholder') or '').lower()
                    if 'people' in ph or 'help' in ph:
                        field.fill(answers.get('people_helped', '300'))
                    elif 'distribut' in ph or 'how' in ph:
                        field.fill(answers.get('distribution_method', 'Homeless'))
            except: pass
            # Select dropdowns
            try:
                for sel in page.locator('select').all():
                    for opt in sel.locator('option').all()[1:]:
                        if 'dock' in opt.inner_text().lower() or 'pallet' in opt.inner_text().lower():
                            sel.select_option(index=sel.locator('option').all().index(opt))
                            break
            except: pass
            # Fill card
            card = org_config.get('card', {})
            try:
                for inp in page.locator('input').all()[:20]:
                    n = (inp.get_attribute('name') or '').lower()
                    if 'number' in n: inp.fill(card.get('number', ''))
                    elif 'exp' in n: inp.fill(card.get('expiry', ''))
                    elif 'cvv' in n or 'cvc' in n: inp.fill(card.get('cvv', ''))
            except: pass
            # Place order
            page.screenshot(path=f"{SCREENSHOT_DIR}/{org_key}_pre_order.png")
            place_btn = page.locator('button:has-text("Place Order"), button:has-text("Complete"), button[type="submit"]').first
            if place_btn.is_visible(timeout=3000):
                place_btn.click()
                time.sleep(3)
            else:
                return 'FAILED', 'Place Order button not found', time.time() - start_time
            page.screenshot(path=f"{SCREENSHOT_DIR}/{org_key}_post_order.png")
            text = page.inner_text('body').lower()
            for ok in ['thank you', 'order confirmed', 'confirmation']:
                if ok in text:
                    return 'SUCCESS', 'Order placed!', time.time() - start_time
            for bad in ['not available', 'sold out']:
                if bad in text:
                    return 'MISSED', 'Sold out during checkout', time.time() - start_time
            return 'MANUAL', 'Check screenshot', time.time() - start_time
        except Exception as e:
            log.error(f"[{org_key}] Error: {e}")
            return 'FAILED', str(e), time.time() - start_time

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
    def do_GET(self):
        if self.path == '/health':
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status':'ok','contexts':list(manager.contexts.keys())}).encode())
        else: self.send_response(404); self.end_headers()
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length',0))))
        if self.path == '/checkout':
            org_key, truck_name, truck_url = body.get('org_key'), body.get('truck_name'), body.get('truck_url')
            try:
                all_orgs = _cfg.load_orgs()
                org_config = all_orgs.get(org_key)
                if not org_config:
                    self.send_response(404); self.end_headers(); return
            except Exception:
                self.send_response(500); self.end_headers(); return
            status, msg, elapsed = manager.checkout(org_key, org_config, truck_name, truck_url)
            self.send_response(200)
            self.send_header('Content-Type','application/json')
            self.end_headers()
            self.wfile.write(json.dumps({'status':status,'message':msg,'elapsed':round(elapsed,1)}).encode())
        elif self.path == '/shutdown':
            manager.shutdown()
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"status":"shutting_down"}')
        else: self.send_response(404); self.end_headers()

def main():
    log.info("Good360 Persistent Browser Daemon v2 starting...")
    manager.start()
    server = HTTPServer(('0.0.0.0', DAEMON_PORT), Handler)
    log.info(f"API on port {DAEMON_PORT} - Ready!")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: manager.shutdown(); server.server_close()

if __name__ == '__main__': main()
