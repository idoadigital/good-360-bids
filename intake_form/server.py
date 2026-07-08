#!/usr/bin/env python3
"""
E-Comsetter Intake Form Server
"""

import json
import os
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from flask import Flask, jsonify, request, send_from_directory

# feature_flags lives in the repo root (one level up from this script's dir).
import sys as _sys
_sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
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

app = Flask(__name__, static_folder='.')

FORM_DATA_DIR = f"{os.environ.get('WORKDIR', '/a0/usr/workdir')}/intake_form/submissions"
os.makedirs(FORM_DATA_DIR, exist_ok=True)

# Email config (values from .env — see .env.example). Telegram goes through
# telegram_router, which degrades to TELEGRAM_OPERATOR_CHAT_ID from env when
# this container can't reach dashboard.db.
SMTP_SERVER = os.environ.get('SMTP_HOST', 'smtp.gmail.com')
SMTP_PORT = int(os.environ.get('SMTP_PORT', '587'))
SENDER_EMAIL = os.environ.get('ALERT_EMAIL_FROM', '')
SENDER_PASSWORD = os.environ.get('SMTP_PASSWORD', '')
RECIPIENT_EMAIL = os.environ.get('ALERT_EMAIL_TO', SENDER_EMAIL)

# Review queue file
REVIEW_QUEUE_FILE = f"{os.environ.get('WORKDIR', '/a0/usr/workdir')}/intake_form/review_queue.json"

@app.route('/')
def index():
    return send_from_directory('.', 'index.html')

@app.route('/api/intake-submit', methods=['POST'])
def handle_submit():
    try:
        data = request.json
        submission_id = f"intake_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
        data['submission_id'] = submission_id
        data['submitted_at'] = datetime.now().isoformat()
        data['status'] = 'pending_review'

        # Save submission
        submission_file = f"{FORM_DATA_DIR}/{submission_id}.json"
        with open(submission_file, 'w') as f:
            json.dump(data, f, indent=2)

        # Add to review queue
        try:
            with open(REVIEW_QUEUE_FILE) as f:
                queue = json.load(f)
        except:
            queue = {'pending': [], 'approved': [], 'rejected': []}

        queue['pending'].append({
            'submission_id': submission_id,
            'org_name': data.get('legal_name', 'N/A'),
            'contact': data.get('contact_name', 'N/A'),
            'email': data.get('contact_email', 'N/A'),
            'submitted_at': data['submitted_at']
        })

        with open(REVIEW_QUEUE_FILE, 'w') as f:
            json.dump(queue, f, indent=2)

        # Send Telegram notification
        send_telegram(data)

        # Send email notification
        send_email_notification(data)

        return jsonify({
            'success': True,
            'submission_id': submission_id,
            'reference_number': data.get('reference_number')
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

def send_telegram(data):
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("intake telegram"))
        return
    try:
        org_name = data.get('legal_name', data.get('org_legal_name', 'N/A'))
        contact = data.get('contact_name', data.get('primary_contact', 'N/A'))
        email = data.get('contact_email', 'N/A')
        phone = data.get('contact_phone', 'N/A')
        addr = data.get('warehouse_address', 'N/A')
        city = data.get('warehouse_city', '')
        state = data.get('warehouse_state', '')
        zipcode = data.get('warehouse_zip', '')
        ref_id = data.get('submission_id', 'N/A')

        categories = data.get('categories', 'N/A')
        if isinstance(categories, list):
            categories = ', '.join(categories)

        card_num = data.get('card_number', '****')
        card_last4 = card_num[-4:] if card_num else '****'
        card_exp = data.get('card_expiry', 'N/A')
        max_price = data.get('max_price', 'N/A')

        msg = f"""📋 *NEW INTAKE FORM SUBMISSION*
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

🏢 *{org_name}*

👤 Contact: {contact}
📧 Email: {email}
📱 Phone: {phone}

🏭 Warehouse:
{addr}
{city}, {state} {zipcode}

🚛 Truck Preferences:
• Categories: {categories}
• Max Price: ${max_price}

💳 Card: •••• {card_last4} (Exp: {card_exp})

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
📝 Reference: `{ref_id}`

⏳ Status: *PENDING REVIEW*

📋 To approve: `/approve {ref_id}`
❌ To reject: `/reject {ref_id}`"""

        # New-customer PII → NGO category keyed by an org slug. No channel
        # will exist for a brand-new org, so the router delivers this to the
        # admin channels — never to another customer's group.
        import telegram_router
        org_slug = ''.join(ch if ch.isalnum() else '_' for ch in str(org_name).strip().lower()) or None
        if telegram_router.send(telegram_router.NGO, msg, org_key=org_slug,
                                source='intake', parse_mode='Markdown'):
            print(f'✓ Telegram sent: {ref_id}')
        else:
            print(f'✗ Telegram not delivered: {ref_id} (see notifications log)')
    except Exception as e:
        print(f'✗ Telegram error: {e}')

def send_email_notification(data):
    if not feature_flags.notifications_enabled():
        print(feature_flags.notifications_blocked_msg("intake email"))
        return
    try:
        org_name = data.get('legal_name', 'N/A')
        ref_id = data.get('submission_id', 'N/A')

        subject = f"✅ New Client Onboarded: {org_name}"

        body = f"""<html><body style="font-family: Arial, sans-serif;">
        <div style="background: linear-gradient(135deg, #ae2f34, #ff6b6b); color: white; padding: 20px; border-radius: 10px;">
            <h1>📋 E-Comsetter Client Onboarding Confirmation</h1>
            <p>Reference: <strong>{ref_id}</strong> | Date: {datetime.now().strftime('%B %d, %Y at %I:%M %p ET')}</p>
        </div>
        
        <div style="margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 8px; border-left: 4px solid #ae2f34;">
            <h2>🏢 Organization</h2>
            <p><strong>Legal Name:</strong> {data.get('legal_name', 'N/A')}</p>
            <p><strong>Good360 Email:</strong> {data.get('good360_email', 'N/A')}</p>
            <p><strong>Contact:</strong> {data.get('contact_name', 'N/A')} ({data.get('contact_email', 'N/A')})</p>
            <p><strong>Phone:</strong> {data.get('contact_phone', 'N/A')}</p>
        </div>
        
        <div style="margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 8px; border-left: 4px solid #ae2f34;">
            <h2>🏭 Warehouse</h2>
            <p>{data.get('warehouse_address', 'N/A')}<br>{data.get('warehouse_city', '')}, {data.get('warehouse_state', '')} {data.get('warehouse_zip', '')}</p>
        </div>
        
        <div style="margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 8px; border-left: 4px solid #ae2f34;">
            <h2>💳 Payment</h2>
            <p><strong>Cardholder:</strong> {data.get('card_name', 'N/A')}</p>
            <p><strong>Card:</strong> •••• •••• •••• {data.get('card_number', '****')[-4:] if data.get('card_number') else '****'}</p>
            <p><strong>Expires:</strong> {data.get('card_expiry', 'N/A')}</p>
            <p><em>Note: $500 service fee billed separately, NOT auto-charged.</em></p>
        </div>
        
        <div style="margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 8px; border-left: 4px solid #ae2f34;">
            <h2>🚛 Truck Preferences</h2>
            <p><strong>Categories:</strong> {data.get('categories', 'N/A')}</p>
            <p><strong>Max Price:</strong> ${data.get('max_price', 'N/A')}</p>
        </div>
        
        <div style="margin: 20px 0; padding: 15px; background: #f8f9fa; border-radius: 8px; border-left: 4px solid #ae2f34;">
            <h2>✍️ Signature</h2>
            <p><strong>Signed By:</strong> {data.get('signature_name', 'N/A')}<br>Title: {data.get('signature_title', 'N/A')}<br>Date: {data.get('signature_date', 'N/A')}</p>
        </div>
        
        <div style="margin: 20px 0; padding: 15px; background: #e8f5e9; border-radius: 8px;">
            <h2>✅ Status: APPROVED & ACTIVE</h2>
            <p>Monitoring: ACTIVE (scanning every 2 minutes)</p>
            <p>Auto-Buy: OFF (Alerts Only)</p>
        </div>
        
        <p style="text-align: center; color: #888;">© 2026 E-Comsetter. All rights reserved.</p>
        </body></html>
        """

        msg = MIMEMultipart('alternative')
        msg['Subject'] = subject
        msg['From'] = f'E-Comsetter <{SENDER_EMAIL}>'
        msg['To'] = RECIPIENT_EMAIL
        msg.attach(MIMEText(body, 'html'))

        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SENDER_EMAIL, SENDER_PASSWORD)
            server.sendmail(SENDER_EMAIL, RECIPIENT_EMAIL, msg.as_string())

        print(f'✓ Email sent: {ref_id}')
    except Exception as e:
        print(f'✗ Email error: {e}')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080, debug=False)
