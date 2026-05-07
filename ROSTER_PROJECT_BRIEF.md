# E-Comsetter Roster System — Project Brief & Architecture
**Created:** 2026-03-20 | **Updated:** 2026-03-26 | **Status:** Active Development
**Location:** /a0/usr/workdir/good360_roster/

---

## 📋 Version History
| Version | Date | Changes |
|---------|------|--------|
| 1.0 | 2026-03-20 | Initial brief created |
| 2.0 | 2026-03-26 | **Major update:** Incorporated learnings from single-org deployment, added critical business rules, fixed discrepancies |

---

## 🎯 What We Are Building
A **multi-nonprofit Good360 truck-finding and auto-purchasing rotation system**.
The existing Good360 monitor (`good360_monitor.py`) already scans for trucks.
This new system adds:
- A secure roster of nonprofit organizations (credentials, addresses, payment cards)
- A 7-day cooldown rotation queue (longest-waiting org buys next)
- Per-org, per-category auto-buy toggles
- Alert-only mode (email/Telegram) when auto-buy is OFF
- Master catch-all admin credit card as last resort (with penalty invoice)
- Revenue model: $200/month subscription + $500 finding fee per truck

---

## 🔐 Design Philosophy
- **Security-first:** SQLCipher (AES-256 DB encryption) + Fernet field-level encryption on passwords/cards
- **Reliability-first:** Every failure has a defined response, nothing silently breaks
- **Modular:** Each component independently testable
- **Future-ready:** Telegram + Email alerts now, Twilio SMS pre-wired

---

## ⚠️ CRITICAL BUSINESS RULES (Learned from Production)

### Rule 1: Single-Purchase Lock
> **Once auto-buy starts for ANY truck, immediately pause all auto-buy for that organization.**

| Scenario | Action |
|----------|--------|
| Truck detected → auto-buy starts | **Lock acquired** — no other trucks can trigger auto-buy |
| SUCCESS | Lock stays, **7-day cooldown** starts |
| MISSED (sold out) | **Lock released immediately** — retry next truck |
| FAILED (checkout error) | **Lock released immediately** — retry next truck |

**Why:** Each org can only buy ONE truck every 7 days. Processing two trucks simultaneously causes checkout conflicts and failures.

### Rule 2: 7-Day Cooldown = Calendar Week (NOT 7×24 hours)
| Purchase Day | Next Allowed |
|--------------|-------------|
| Wednesday, March 25 | **Wednesday, April 1** (any time) |
| Friday, March 27 | **Wednesday, April 2** (any time) |
| Monday, March 30 | **Wednesday, April 6** (any time) |

**Why:** Good360 resets weekly. Timestamp-based cooldown (7×24h) caused timing issues.

### Rule 3: 3-State Success Detection (NO FALSE POSITIVES)
```python
if order_confirmation_detected():  # "Thank you" + order number + payment confirmed
    return "SUCCESS"  # ✅ Real purchase
elif truck_sold_out() and not checkout_completed():  # "Not available" before checkout
    return "MISSED"  # ⚠️ Truck gone, retry next
else:  # Checkout attempted but failed
    return "FAILED"  # ❌ Error occurred
```

**Why:** The bot previously sent "ORDER COMPLETE" when trucks sold out mid-checkout. Never trust success without verification.

### Rule 4: Crash Recovery & Timeout Protection
- **90-second timeout** on checkout operations (previously 30s — too short)
- **Try/except with Telegram alert** before exiting on crash
- **Watchdog** monitors system health and alerts within 5 minutes of failure

### Rule 5: Alert Channels Are NOT Optional
- Always alert on: SUCCESS, FAILED, MISSED, CRASH, SYSTEM_DOWN
- Channels: **Telegram** (primary) + **Email** (secondary)
- No silent failures — every truck detection gets a response

---

## 📁 Target File Structure
```
/a0/usr/workdir/
├── good360_monitor.py              ← Existing scanner (cron, every 1-2 min)
├── good360_autobuy.py              ← Current autobuy (Hope 4 Humanity only)
├── good360_watchdog.py             ← Health monitor (15-min checks)
├── good360_telegram_bot.py         ← @Good360mr_bot (Mission Control)
├── good360_checkout_config.json    ← Current checkout config
├── ARCHITECTURE.md                 ← Living documentation (auto-updated)
├── good360_roster/                 ← NEW multi-org system
│   ├── db/
│   │   └── roster.db               ← SQLCipher encrypted database
│   ├── vault.py                    ← Encryption/decryption
│   ├── queue_manager.py            ← Rotation + cooldown logic
│   ├── roster_orchestrator.py      ← Main coordinator
│   ├── notifier.py                 ← Alerts (Email + Telegram + Twilio-ready)
│   ├── billing_manager.py          ← Fee recording + invoicing
│   ├── health_checker.py           ← Daily credential verification
│   ├── admin_cli.py                ← CLI to add/manage orgs
│   ├── good360_autobuy_v2.py       ← Multi-account checkout with Single-Purchase Lock
│   ├── dashboard.py                ← Web dashboard (served via Cloudflare Tunnel)
│   ├── dashboard_data.py           ← Dashboard API backend
│   └── templates/
│       └── dashboard.html          ← Dashboard UI
└── .env                            ← ROSTER_MASTER_KEY + Telegram tokens
```

---

## 🗄️ Database Schema (SQLite + SQLCipher)

### Table: nonprofits
- id, uuid, org_name, contact_name, contact_email, contact_phone
- joined_date, status (active/cooldown/suspended/pending_setup)
- queue_position, last_purchase_date, cooldown_until (next allowed Wednesday)
- total_trucks_found, subscription_active, subscription_start
- alert_email (may differ from login)
- phone_number (E.164 format, e.g. +14045551234)
- telegram_chat_id (for Telegram alerts)
- sms_alerts_enabled (0/1, Twilio-ready)
- auto_buy_global (master ON/OFF switch)
- single_purchase_lock (0/1 — internal flag, locked during checkout)
- lock_acquired_at (timestamp of when lock was set)
- master_card_fallback (allow admin card as last resort?)
- agreement_signed, agreement_signed_date
- max_price_override (NULL = use system default $6400)
- notes, created_at, updated_at

### Table: nonprofit_credentials
- id, nonprofit_id (FK)
- email (Good360 login email)
- password_enc (BLOB, Fernet-encrypted)
- good360_org_id, last_login, login_verified

### Table: nonprofit_addresses
- id, nonprofit_id (FK)
- address_label, street_line1, street_line2
- city, state, zip_code, country
- is_primary

### Table: nonprofit_payment_methods (up to 3 per org)
- id, nonprofit_id (FK)
- priority (1=primary, 2=backup1, 3=backup2)
- card_holder_name
- card_number_enc (BLOB, Fernet-encrypted)
- card_last4 (plaintext, display only)
- card_expiry_month, card_expiry_year
- card_cvv_enc (BLOB, Fernet-encrypted)
- billing_zip, card_type, is_active
- last_used, last_declined, decline_count

### Table: nonprofit_category_preferences
- id, nonprofit_id (FK)
- category_key (amazon_new_unsorted / amazon_variety / amazon_houseware / softlines / other)
- category_label (human-readable)
- auto_buy_enabled (1=auto-buy, 0=alert-only)
- max_price_override (org+category-specific price cap)
- is_excluded (1=never alert or buy this category)

### Table: nonprofit_checkout_answers
- id, nonprofit_id (FK)
- question_key, question_label
- answer_text (their unique answer to Good360 checkout questions)
- field_type (text/select/checkbox/textarea)
- sort_order

### Table: truck_events
- id, uuid, detected_at
- truck_title, truck_url, truck_price, truck_location, truck_category
- raw_data_json
- status (detected/assigned/purchased/missed/unavailable)
- assigned_to_org_id (FK), assigned_at, notes
- purchase_result (success/failed/missed — 3-state detection)
- error_message, screenshot_path (evidence for debugging)

### Table: purchase_attempts
- id, uuid, truck_event_id (FK), nonprofit_id (FK)
- payment_method_id (FK)
- started_at, completed_at
- status: in_progress / success / success_mastercard / failed_payment /
          failed_login / failed_checkout / truck_gone / alerted_manual /
          skipped_excluded / retrying / **missed_sold_out** (NEW)
- mode: auto_buy / alert_only / master_card_fallback
- attempt_number, error_message, screenshot_path
- confirmation_number, order_total, cooldown_applied
- exit_code (0=SUCCESS, 1=FAILED, 2=MISSED, 3=MANUAL, 4=COOLDOWN, 5=LOCKED)

### Table: system_payment_methods (E-Comsetter admin/master cards)
- id, card_label, card_holder_name
- card_number_enc (BLOB, Fernet-encrypted)
- card_last4, card_expiry_month, card_expiry_year
- card_cvv_enc (BLOB, Fernet-encrypted)
- billing_zip, billing_address, card_type
- is_active, priority, total_charged, last_used

### Table: master_card_transactions
- id, purchase_attempt_id (FK), nonprofit_id (FK), system_payment_id (FK)
- truck_price, penalty_multiplier (default 3.0)
- penalty_amount, total_org_owes
- invoice_generated, invoice_number, reimbursed, reimbursed_date

### Table: agreements
- id, nonprofit_id (FK)
- agreement_type: platform_terms / auto_buy_consent / master_card_consent / subscription_recurring
- version, signed_by_name, signed_by_email, signed_at
- ip_address, signature_hash, document_path, is_active

### Table: billing_records
- id, nonprofit_id (FK)
- billing_type (subscription / finding_fee)
- amount, currency, billing_date, due_date, paid_date
- status (pending/paid/overdue/waived)
- purchase_attempt_id (FK, for finding fees)
- invoice_number, notes

### Table: system_events (audit log)
- id, event_type, severity (info/warning/error/critical)
- nonprofit_id (FK), message, metadata_json, created_at

### Table: system_config (key-value)
Key settings:
- cooldown_days = 7
- cooldown_type = "calendar_week" (NOT timestamp-based)
- max_orgs_active = 10
- subscription_fee_usd = 200
- finding_fee_usd = 500
- max_payment_fallbacks = 3
- scanner_interval_min = 1 (updated from 2)
- checkout_timeout_sec = 90 (updated from 30)
- master_card_penalty_multiplier = 3.0
- master_card_enabled = 1
- telegram_alerts_enabled = 1
- email_alerts_enabled = 1
- sms_alerts_enabled = 0 (Twilio future)
- watchdog_interval_min = 15
- alert_only_expiry_minutes = 30
- default_max_price = 6400

---

## 🔄 Core Decision Flow (When Truck Detected)

```
1. Scanner fires → log truck_events
2. Check Single-Purchase Lock:
   - If LOCKED for this org → skip (another truck in progress)
   - If COOLDOWN active → skip (7-day calendar week rule)
3. Queue Manager: who is #1 available? (sorted by last_purchase_date ASC)
4. Check org category preferences:
   - is_excluded? → skip silently
   - auto_buy_enabled? → proceed to checkout
   - auto_buy OFF? → send email + Telegram alert → log as alerted_manual
     → 30min timer → no cooldown if missed
5. **ACQUIRE SINGLE-PURCHASE LOCK** ← CRITICAL STEP
6. Auto-buy: load credentials from vault → logout → login as org
7. Checkout with Card #1 → fail → Card #2 → fail → Card #3
8. All cards fail + master_card_fallback=1 + agreement signed:
   → Use E-Comsetter admin card → penalty invoice (3x finding fee)
9. 3-STATE DETECTION:
   - SUCCESS: set 7-day cooldown (next Wednesday) → rotate queue → billing record → alerts
   - MISSED: release lock immediately → alert "truck sold out" → retry next truck
   - FAILED: release lock immediately → alert error → retry next truck
```

---

## 🔔 Alert Channels

| Channel | Status | Use Case |
|---------|--------|----------|
| **Telegram** | ✅ Active (primary) | Real-time alerts, /status commands, Mission Control |
| **Email** | ✅ Active (secondary) | Status reports, purchase confirmations |
| **Twilio SMS** | 🔮 Future | SMS alerts when Telegram not available |

### Alert Types
| Type | When | Channels |
|------|------|----------|
| ✅ SUCCESS | Purchase completed | Telegram + Email |
| ⚠️ MISSED | Truck sold out during checkout | Telegram + Email |
| ❌ FAILED | Checkout error | Telegram + Email |
| 🚨 CRASH | System crash/restart | Telegram + Email |
| 🛡️ SYSTEM_DOWN | Watchdog detects outage | Telegram + Email |
| 📊 DAILY_REPORT | Summary of activity | Email |

---

## 🃏 Master Card Rules
- Prerequisites: master_card_fallback=1 + master_card_consent agreement signed + all 3 org cards declined
- Log to master_card_transactions
- Penalty = finding_fee × penalty_multiplier (default: $500 × 3 = $1,500)
- Invoice = truck_price + penalty_amount
- 7-day cooldown STILL applies to org
- Alert Serge/admin immediately

---

## 🏗️ Build Order (Start Here)
1. vault.py — AES-256 + Fernet encryption, master key from env
2. roster.db schema — all tables above (include new Single-Purchase Lock fields)
3. queue_manager.py — rotation, cooldown, 7-day calendar week logic
4. notifier.py — Email + Telegram + Twilio-ready
5. admin_cli.py — add/edit/list orgs
6. good360_autobuy_v2.py — credential-parameterized checkout with Single-Purchase Lock
7. roster_orchestrator.py — ties everything together
8. Integration with existing good360_monitor.py (send signal to orchestrator)

---

## 💰 Revenue Model
- $200/month per org subscription
- $500 per successful truck find (finding fee)
- Penalty invoice when master card used: truck_price + (3 × $500)
- Target: 1 truck/week per org
- 10 orgs = ~$7,600–$17,000/month gross at 99% margin

---

## 📡 Remote Access (Cloudflare Tunnel)

The system is accessible remotely via Cloudflare Tunnel:
- **Permanent URL:** https://lechat.quicklybid.com
- **Tunnel ID:** 60647f2a-856c-4510-b6a9-2e44b5880012
- **Token:** /a0/usr/workdir/永久_tunnel_token.txt
- **API Token:** cfat_wGd3rU06Ya8OidIxhupggpGVTeHuPbseyu91ubH0cb281704

---

## ⚠️ Existing System (DO NOT BREAK)
- /a0/usr/workdir/good360_monitor.py — scanner (cron, every 1-2min)
- /a0/usr/workdir/good360_autobuy.py — current autobuy (Hope 4 Humanity only)
- /a0/usr/workdir/good360_watchdog.py — watchdog (15-min health checks)
- /a0/usr/workdir/good360_telegram_bot.py — @Good360mr_bot (Mission Control)
- /a0/usr/workdir/good360_checkout_config.json — current checkout config
- /a0/usr/workdir/ARCHITECTURE.md — living documentation (auto-updated)
- Cron: /etc/cron.d/good360 — DO NOT MODIFY without testing

---

## 🚀 Implementation Phases

### Phase 1: Core Roster (Current)
- [x] Vault encryption
- [x] Database schema
- [x] Queue manager
- [ ] Single-Purchase Lock integration
- [ ] 3-state success detection in autobuy_v2

### Phase 2: Multi-Org Support
- [ ] Admin CLI for adding orgs
- [ ] Per-org credentials and payment methods
- [ ] Per-org category preferences

### Phase 3: Dashboard & Monitoring
- [ ] Web dashboard via Cloudflare Tunnel
- [ ] Real-time truck availability
- [ ] Transaction history
- [ ] Cooldown countdown

### Phase 4: Revenue & Billing
- [ ] Subscription management
- [ ] Finding fee invoicing
- [ ] Master card penalty tracking

---

## 🧠 Critical Learnings (From Production Deployment)

| Learning | Impact | Rule |
|----------|--------|------|
| Never trust success without verification | FALSE POSITIVES cost trucks | Always verify order confirmation page |
| Timezones will bite you | System stopped at 7 PM instead of 11 PM | Always compare ET to ET, not ET to UTC |
| Alerts must survive crashes | Crash = no alert = silent failure | Try/except with Telegram alert before exit |
| Speed kills or saves deals | Trucks sell out in <2 seconds | Reduce checkout time, no unnecessary waits |
| Screenshots don't lie | Proved false positive root cause | Capture evidence at every step |
| False positives worse than false negatives | User lost trust in system | Better to say "missed" than wrong "success" |
| Single-purchase lock prevents chaos | Two simultaneous checkouts broke everything | Lock after first truck detected, release on MISS/FAIL |
| Calendar week cooldown works | Timestamp-based cooldown caused timing issues | Reset on Wednesday, not 7×24 hours |

---

*This is a living document. Update after each major fix or learning.*
