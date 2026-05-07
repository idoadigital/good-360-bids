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

# Load config
with open('good360_checkout_config.json') as f:
    config = json.load(f)

# Constants
LOGIN_URL = "https://marketplace.good360.org/login"
MAX_AUTO_PAY = config.get('max_auto_pay', config.get('max_auto_pay_amount', 6400))
USERNAME = config.get('username', '')
PASSWORD = config.get('password', '')
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

def check_order_confirmation(page_text):
    """Check if order actually completed - TRUE SUCCESS indicators"""
    success_indicators = [
        'thank you for your order',
        'thank you for your purchase',
        'order confirmed',
        'order number',
        'order #',
        'your order has been placed',
        'order receipt',
        'payment successful',
        'checkout complete'
    ]
    page_lower = page_text.lower()
    for indicator in success_indicators:
        if indicator in page_lower:
            return True, indicator
    return False, None

def check_truck_missed(page_text):
    """Check if truck sold out during checkout - TRUCK MISSED"""
    missed_indicators = [
        'not available at the moment',
        'not available',
        'sold out',
        'out of stock',
        'no longer available'
    ]
    page_lower = page_text.lower()
    for indicator in missed_indicators:
        if indicator in page_lower:
            return True, indicator
    return False, None

def autobuy_truck(truck_name, truck_url, screenshot_dir='checkout_screenshots'):
    """Execute auto-buy with proper success/failed/missed detection"""
    print(f"[AUTO-BUY] Starting checkout for: {truck_name}")
    print(f"[AUTO-BUY] URL: {truck_url}")
    print(f"[AUTO-BUY] Max auto-pay: ${MAX_AUTO_PAY}")

    os.makedirs(screenshot_dir, exist_ok=True)

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.set_default_timeout(60000)  # 60s timeout (increased from 30s)

            # Step 1: Login
            print("[AUTO-BUY] Step 1: Logging in...")
            page.goto(LOGIN_URL, wait_until='domcontentloaded')
            time.sleep(1)  # Reduced wait

            # Click Login button to open modal
            try:
                page.click('button:has-text("Login")', timeout=10000)
                time.sleep(0.5)
            except:
                pass

            # Fill credentials
            page.wait_for_selector('input[placeholder*="email" i]', timeout=15000)
            page.fill('input[placeholder*="email" i]', USERNAME)
            page.fill('input[placeholder*="password" i]', PASSWORD)
            time.sleep(0.3)

            # Click Sign in
            page.click('button:has-text("Sign in"), button:has-text("Log in"), button[type="submit"]')
            time.sleep(3)  # Wait for login

            log_step(1, 'login', screenshot_dir, page)

            # Step 2: Navigate to truck page
            print("[AUTO-BUY] Step 2: Going to truck page...")
            page.goto(truck_url, wait_until='domcontentloaded')
            time.sleep(2)

            # Check if truck is available
            page_text = page.content()
            missed, indicator = check_truck_missed(page_text)
            if missed:
                print(f"[AUTO-BUY] ⚠️ TRUCK MISSED - Already sold out: '{indicator}'")
                browser.close()
                return 'MISSED', f"Truck sold out before checkout: {indicator}"

            log_step(3, 'truck_page', screenshot_dir, page)

            # Step 3: Add to cart
            print("[AUTO-BUY] Step 3: Adding to cart...")
            try:
                page.click('button:has-text("Add to cart"), button:has-text("Add to Cart")', timeout=10000)
                time.sleep(1.5)
                log_step(4, 'add_to_cart', screenshot_dir, page)
            except Exception as e:
                print(f"[AUTO-BUY] Failed to add to cart: {e}")
                browser.close()
                return 'FAILED', f"Could not add to cart: {e}"

            # Step 4: Go to checkout
            print("[AUTO-BUY] Step 4: Going to checkout...")
            try:
                page.click('a:has-text("Checkout"), button:has-text("Checkout")', timeout=10000)
                time.sleep(2)
                log_step(5, 'checkout_start', screenshot_dir, page)
            except:
                # Try direct navigation
                page.goto('https://marketplace.good360.org/cart', wait_until='domcontentloaded')
                time.sleep(2)
                try:
                    page.click('a:has-text("Checkout"), button:has-text("Checkout")', timeout=10000)
                    time.sleep(2)
                except Exception as e:
                    print(f"[AUTO-BUY] Failed to reach checkout: {e}")
                    browser.close()
                    return 'FAILED', f"Could not reach checkout page: {e}"

            # Check for truck missed during checkout
            page_text = page.content()
            missed, indicator = check_truck_missed(page_text)
            if missed:
                print(f"[AUTO-BUY] ⚠️ TRUCK MISSED - Sold out during checkout: '{indicator}'")
                browser.close()
                return 'MISSED', f"Truck sold out during checkout: {indicator}"

            # Step 5: Fill shipping address (select warehouse)
            print("[AUTO-BUY] Step 5: Selecting shipping address...")
            time.sleep(1)
            try:
                # Look for warehouse/1025 Progress option
                options = page.locator('select option, .address-option, [data-address]')
                for i in range(options.count()):
                    text = options.nth(i).inner_text()
                    if '1025' in text or 'warehouse' in text.lower():
                        options.nth(i).click()
                        break
                time.sleep(0.5)
                # Click Continue
                page.click('button:has-text("Continue"), button:has-text("Next")', timeout=5000)
                time.sleep(1)
            except:
                pass

            log_step(6, 'after_shipping', screenshot_dir, page)

            # Step 6: Fill checkout questions
            print("[AUTO-BUY] Step 6: Answering questions...")
            time.sleep(1)
            try:
                textareas = page.locator('textarea')
                count = textareas.count()
                if count >= 4:
                    textareas.nth(0).fill(ANSWERS['number_of_people'])
                    textareas.nth(1).fill(ANSWERS['distribution_method'])
                    textareas.nth(2).fill(ANSWERS['warehouse_address'])
                    textareas.nth(3).fill(ANSWERS['pallet_jack'])
                time.sleep(0.5)
                page.click('button:has-text("Continue"), button:has-text("Next")', timeout=5000)
                time.sleep(1)
            except Exception as e:
                print(f"[AUTO-BUY] Questions error: {e}")

            log_step(7, 'questions_filled', screenshot_dir, page)

            # Step 7: Payment page
            print("[AUTO-BUY] Step 7: Entering payment details...")
            time.sleep(1)

            # Extract and display fees
            page_text = page.content()
            admin_fee, shipping_fee, total = extract_total(page_text)
            print(f"[AUTO-BUY] Admin fee: ${admin_fee}, Shipping: ${shipping_fee}, Total: ${total}")

            if total > MAX_AUTO_PAY:
                msg = f"⚠️ MANUAL PURCHASE REQUIRED\n\n{truck_name}\nTotal: ${total} (exceeds ${MAX_AUTO_PAY} limit)\n\n🔗 {truck_url}"
                send_telegram(msg)
                browser.close()
                return 'MANUAL', f"Total ${total} exceeds limit ${MAX_AUTO_PAY}"

            log_step(8, 'payment_page', screenshot_dir, page)

            # Fill card details
            try:
                # Select Visa
                page.click('button:has-text("Visa"), [data-card="visa"]', timeout=5000)
                time.sleep(0.5)

                # Fill card number, expiry, CVV
                card_number = _os.environ.get('CARD_HOPE4HUMANITY_NUMBER', '')
                card_expiry_raw = _os.environ.get('CARD_HOPE4HUMANITY_EXPIRY', '')  # MMYY
                card_cvv = _os.environ.get('CARD_HOPE4HUMANITY_CVV', '')
                if not (card_number and card_expiry_raw and card_cvv):
                    raise RuntimeError('Card details missing from env — refusing to submit empty payment form')
                card_expiry = f"{card_expiry_raw[:2]}/{card_expiry_raw[2:]}"  # MMYY -> MM/YY

                card_input = page.locator('input[name*="card"], input[placeholder*="card"], input[placeholder*="number"]')
                if card_input.count() > 0:
                    card_input.first.fill(card_number)

                expiry_input = page.locator('input[name*="exp"], input[placeholder*="exp"], input[placeholder*="MM"]')
                if expiry_input.count() > 0:
                    expiry_input.first.fill(card_expiry)

                cvv_input = page.locator('input[name*="cvv"], input[placeholder*="cvv"], input[placeholder*="security"]')
                if cvv_input.count() > 0:
                    cvv_input.first.fill(card_cvv)

                time.sleep(0.5)
            except Exception as e:
                print(f"[AUTO-BUY] Card fill error: {e}")

            log_step(9, 'cc_filled', screenshot_dir, page)

            # Step 8: Select billing address from dropdown
            print("[AUTO-BUY] Step 8: Selecting billing address...")
            try:
                # Click billing dropdown
                page.click('.billing-address select, select[name*="billing"], .address-select', timeout=5000)
                time.sleep(0.5)

                # Select 267 Langley Dr address
                options = page.locator('select option')
                for i in range(options.count()):
                    text = options.nth(i).inner_text()
                    if '267' in text or 'Langley' in text or '30046' in text:
                        options.nth(i).click()
                        print(f"[AUTO-BUY] Selected: {text}")
                        break
                time.sleep(1)
            except Exception as e:
                print(f"[AUTO-BUY] Billing address error: {e}")

            log_step(10, 'billing_selected', screenshot_dir, page)

            # Step 9: Place order
            print("[AUTO-BUY] Step 9: Placing order...")
            try:
                page.click('button:has-text("Place order"), button:has-text("Place Order"), button:has-text("Submit")', timeout=10000)
                time.sleep(5)  # Wait for order processing
            except Exception as e:
                print(f"[AUTO-BUY] Place order error: {e}")
                browser.close()
                return 'FAILED', f"Could not click Place Order: {e}"

            log_step(11, 'after_place_order', screenshot_dir, page)

            # Step 10: VERIFY ORDER COMPLETION
            print("[AUTO-BUY] Step 10: Verifying order...")
            page_text = page.content()

            # Check for TRUE SUCCESS
            is_confirmed, indicator = check_order_confirmation(page_text)

            if is_confirmed:
                print(f"[AUTO-BUY] ✅ ORDER CONFIRMED! Indicator: '{indicator}'")
                success_msg = f"✅ AUTO-BUY COMPLETE!\n\n{truck_name}\nTotal: ${total}\n\nPurchase successful - check your email for confirmation!\n\n— E-Comsetter Auto-Buy"
                send_telegram(success_msg)
                browser.close()
                return 'SUCCESS', f"Order confirmed: {indicator}"

            # Check for TRUCK MISSED
            missed, indicator = check_truck_missed(page_text)
            if missed:
                print(f"[AUTO-BUY] ⚠️ TRUCK MISSED - Sold out during checkout: '{indicator}'")
                browser.close()
                return 'MISSED', f"Truck sold out: {indicator}"

            # Otherwise it's a FAILURE
            print("[AUTO-BUY] ❌ ORDER NOT CONFIRMED - No success indicators found")
            fail_msg = f"❌ AUTO-BUY FAILED\n\n{truck_name}\nTotal: ${total}\n\nOrder could not be completed. Manual purchase may be needed.\n\n🔗 {truck_url}\n\n— E-Comsetter Auto-Buy"
            send_telegram(fail_msg)
            browser.close()
            return 'FAILED', "Order confirmation not found after Place Order"

    except PlaywrightTimeout as e:
        error_msg = f"Timeout: {str(e)[:100]}"
        print(f"[AUTO-BUY] ❌ {error_msg}")
        send_telegram(f"❌ AUTO-BUY TIMEOUT\n\n{truck_name}\n{error_msg}\n\n— E-Comsetter")
        return 'FAILED', error_msg

    except Exception as e:
        error_msg = f"Error: {str(e)[:100]}"
        print(f"[AUTO-BUY] ❌ {error_msg}")
        traceback.print_exc()
        send_telegram(f"❌ AUTO-BUY ERROR\n\n{truck_name}\n{error_msg}\n\n— E-Comsetter")
        return 'FAILED', error_msg

if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python good360_autobuy.py <truck_name> <truck_url>")
        sys.exit(1)

    truck_name = sys.argv[1]
    truck_url = sys.argv[2]

    result, message = autobuy_truck(truck_name, truck_url)
    print(f"\n[AUTO-BUY] RESULT: {result} - {message}")

    # Exit codes: 0=success, 1=failed, 2=missed, 3=manual
    exit_codes = {'SUCCESS': 0, 'FAILED': 1, 'MISSED': 2, 'MANUAL': 3}
    sys.exit(exit_codes.get(result, 1))
