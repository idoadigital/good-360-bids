# E-Comsetter Good360 Mission Control
## Architectural Learning & System Documentation

**Version**: 4.0  
**Last Updated**: 2026-03-25  
**Owner**: E-Comsetter / Hope 4 Humanity  

---

## 📋 Table of Contents
1. [System Overview](#system-overview)
2. [Architecture Diagram](#architecture-diagram)
3. [Component Documentation](#component-documentation)
4. [Bug History & Fixes](#bug-history--fixes)
5. [Critical Learnings](#critical-learnings)
6. [Alert Strategy](#alert-strategy)
7. [Cost Optimization](#cost-optimization)
8. [Lessons Learned](#lessons-learned)
9. [Future Roadmap](#future-roadmap)

---

## 🏗️ System Overview

### Mission
Automate Good360 Amazon truckload purchasing with:
- Real-time monitoring (every 1 minute)
- Automatic checkout for qualifying trucks
- Multi-channel alerts (Email + Telegram)
- Health monitoring with watchdog

### Business Rules
| Rule | Value |
|------|-------|
| Auto-buy threshold | $6,400 max total |
| Active hours | Mon-Fri 6AM-11PM ET |
| Target trucks | New Unsorted, Variety, Houseware |
| Excluded trucks | Softlines (ignored) |
| Card on file | Visa ****7421 (Berneitha James) |
| Chrome card name | "Kingdom" |
| Billing address | 267 Langley Dr, Lawrenceville GA 30046 |
| Warehouse | 1025 Progress Circle, Lawrenceville GA 30043 |

---

## 🏛️ Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────┐
│                    E-COMSETTER MISSION CONTROL                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌──────────────┐    ┌──────────────┐    ┌──────────────┐     │
│  │   CRON       │    │   WATCHDOG   │    │   TELEGRAM   │     │
│  │  (every 1m)  │───▶│  (every 15m) │    │     BOT      │     │
│  └──────┬───────┘    └──────┬───────┘    └──────┬───────┘     │
│         │                   │                   │              │
│         ▼                   ▼                   ▼              │
│  ┌──────────────────────────────────────────────────────┐     │
│  │              good360_monitor.py                       │     │
│  │  1. Login to Good360                                  │     │
│  │  2. Scrape truck availability                         │     │
│  │  3. Check against auto-buy rules                      │     │
│  │  4. Trigger alerts or auto-buy                        │     │
│  └───────────────────────┬──────────────────────────────┘     │
│                          │                                      │
│                          ▼                                      │
│  ┌──────────────────────────────────────────────────────┐     │
│  │              good360_autobuy.py                        │     │
│  │  1. Navigate to truck page                            │     │
│  │  2. Add to cart                                       │     │
│  │  3. Answer 4 checkout questions                       │     │
│  │  4. Enter payment details                             │     │
│  │  5. Select billing from dropdown                      │     │
│  │  6. Place order                                       │     │
│  │  7. Verify with 3-state detection                     │     │
│  └──────────────────────────────────────────────────────┘     │
│                                                                 │
│  ┌──────────────────────────────────────────────────────┐     │
│  │              ALERT CHANNELS                            │     │
│  │  • Telegram (Good360mr_bot → -1003866351492)          │     │
│  │  • Email (berneitha@hope4humanity.us)                  │     │
│  │  • Email (sdibao@gmail.com)                            │     │
│  └──────────────────────────────────────────────────────┘     │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📦 Component Documentation

### 1. good360_monitor.py
**Purpose**: Main monitoring and orchestration script

**Key Functions**:
- `check_trucks()` - Scrapes Good360 for truck availability
- `run_autobuy()` - Executes auto-buy with 3-state result handling
- `is_autobuy_active()` - Checks pause flag file
- `send_alert()` - Sends notifications via Telegram/email

**State Files**:
- `good360_run_log.json` - History of all monitoring runs
- `good360_paused.flag` - Pause auto-buy while keeping alerts
- `good360_alerted_state.json` - Prevents duplicate alerts

### 2. good360_autobuy.py
**Purpose**: Handles complete checkout flow

**3-State Detection Logic** (CRITICAL - Implemented after false positive bugs):
```python
def check_order_confirmation(page_text):
    """TRUE SUCCESS - only these indicators confirm purchase"""
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

def check_truck_missed(page_text):
    """TRUCK MISSED - sold out during checkout"""
    missed_indicators = [
        'not available at the moment',
        'not available',
        'sold out',
        'out of stock',
        'no longer available'
    ]
```

**Exit Codes**:
| Code | State | Meaning |
|------|-------|--------|
| 0 | SUCCESS | Order confirmed with real indicators |
| 1 | FAILED | Checkout attempted but failed |
| 2 | MISSED | Truck sold out during checkout |
| 3 | MANUAL | Exceeds limit, manual purchase needed |

### 3. good360_watchdog.py
**Purpose**: Health monitoring - ensures system is running

**Key Features**:
- Checks every 15 minutes during business hours
- Compares last scan time against current ET time
- Sends alert if no scan in 5+ minutes
- Sends recovery notification when back online

**CRITICAL: Timezone Handling**:
```python
ET = pytz.timezone('America/New_York')

# Log timestamps are in ET - must compare ET to ET
last_dt = datetime.strptime(ts_from_log, '%Y-%m-%d %H:%M:%S')  # Naive ET
now_et = datetime.now(ET).replace(tzinfo=None)  # Current ET as naive
minutes_ago = int((now_et - last_dt).total_seconds() / 60)
```

### 4. good360_telegram_bot.py
**Purpose**: Mission Control interface via Telegram

**Commands**:
| Command | Function |
|---------|----------|
| `/status` | System health, last scan, auto-buy status |
| `/logs` | Last 10 monitoring runs |
| `/test` | Trigger immediate manual scan |
| `/pause` | Pause auto-buy (alerts still active) |
| `/resume` | Resume auto-buy |
| `/help` | Show all commands |

**Bot Identity**:
- Token: `<TELEGRAM_BOT_TOKEN>`
- Chat ID: `-1003866351492`
- Bot Name: `Good360mr_bot`

---

## 🐛 Bug History & Fixes

### Bug #1: False Positive Alerts (CRITICAL)
**Date**: 2026-03-25  
**Severity**: CRITICAL - Cost user trucks and money  

**Problem**: System reported "ORDER COMPLETE" when no purchase was made.

**Root Cause**: 
- Bot navigated to truck page after detecting availability
- Truck sold out in <2 seconds
- Bot saw "Not available at the moment" on product page
- Bot incorrectly interpreted this as "success" (nothing to buy = success)

**Evidence**: Screenshots showed truck product page (not checkout confirmation) when "success" was reported.

**Fix**:
1. Implemented 3-state detection (SUCCESS/FAILED/MISSED)
2. TRUE SUCCESS only triggers on actual order confirmation page
3. "Not available" now triggers TRUCK MISSED alert
4. Added proper screenshots at each checkout step

---

### Bug #2: Timezone Bug in Watchdog
**Date**: 2026-03-24  
**Severity**: HIGH - Watchdog showed false "healthy" status  

**Problem**: Watchdog showed "last scan 1 min ago" when scan was actually hours ago.

**Root Cause**:
- Log timestamps are in ET (per cron TZ setting)
- Watchdog was using `datetime.now()` which returns server time (UTC)
- Comparing ET timestamps to UTC time gave wrong results

**Fix**:
```python
# CORRECT:
now_et = datetime.now(ET).replace(tzinfo=None)  # Current time in ET

# WRONG (caused bug):
now = datetime.now()  # Server time (UTC)
```

---

### Bug #3: Auto-buy Timeout Crash
**Date**: 2026-03-25  
**Severity**: CRITICAL - System died silently  

**Problem**: Auto-buy script timed out after 30 seconds, crashed the monitor, no alerts sent.

**Root Cause**:
- `page.set_default_timeout(30000)` too short for slow Good360 pages
- No try/except around critical operations
- No failure alert sent on crash

**Fix**:
1. Increased timeout to 60000ms (60 seconds)
2. Added try/except blocks with Telegram alerts
3. Added crash recovery handlers

---

### Bug #4: Bot Conflict Errors
**Date**: 2026-03-23  
**Severity**: MEDIUM - Bot wouldn't start  

**Problem**: Telegram API returned "Conflict: terminated by other getUpdates request"

**Root Cause**: Multiple bot instances running simultaneously

**Fix**:
1. Kill all instances: `pkill -9 -f good360_telegram_bot.py`
2. Wait 20 seconds for Telegram session release
3. Start exactly ONE clean instance
4. Track PID in `good360_bot.pid`

---

### Bug #5: Missing Credentials
**Date**: 2026-03-24  
**Severity**: CRITICAL - Auto-buy couldn't function  

**Problem**: `checkout_config.json` had "NOT SET" for username, password, warehouse.

**Root Cause**: Config file was never populated with actual credentials.

**Fix**: Updated config with:
- Username: `berneitha@hope4humanity.us`
- Password: stored securely
- Warehouse: `1025 Progress Circle, Lawrenceville GA 30043`

---

### Bug #6: Billing Address Manual Entry
**Date**: 2026-03-24  
**Severity**: MEDIUM - Checkout failed  

**Problem**: Bot tried to type billing address manually instead of selecting from dropdown.

**Root Cause**: Good360 uses a dropdown for billing address selection, not manual input.

**Fix**: Updated script to:
1. Click billing address dropdown
2. Find option containing "267 Langley" or "30046"
3. Click to select
4. Verify "Berneitha James" appears after selection

---

## 🔑 Critical Learnings

### 1. Never Trust "Success" Without Verification
**Lesson**: Always verify with real confirmation indicators before declaring success.

**Implementation**:
- Check for specific phrases: "thank you", "order number", "order #"
- Screenshot every step for evidence
- Differentiate between "nothing to buy" and "purchase complete"

### 2. Timezones Will Bite You
**Lesson**: Always compare timestamps in the same timezone.

**Implementation**:
- Use `TZ=America/New_York` in cron
- Convert all times to ET before comparison
- Use `.replace(tzinfo=None)` to strip timezone for naive comparison

### 3. Alerts Must Survive Crashes
**Lesson**: If the script crashes, the alert must still go out.

**Implementation**:
- Wrap critical operations in try/except
- Send alert in except block before returning
- Use watchdog as backup health monitor

### 4. Speed Kills (Or Saves) Deals
**Lesson**: Trucks sell out in <2 seconds. Every millisecond counts.

**Implementation**:
- Minimize wait times between steps
- Use `wait_until='domcontentloaded'` instead of `networkidle`
- Pre-fill all forms as fast as possible

### 5. Screenshots Don't Lie
**Lesson**: When debugging, screenshots are your best evidence.

**Implementation**:
- Take screenshot at every checkout step
- Name files with timestamp and step number
- Review screenshots to find exactly where things went wrong

### 6. False Positives Are Worse Than False Negatives
**Lesson**: Telling user "success" when nothing happened creates false confidence.

**Implementation**:
- 3-state detection (not just success/fail)
- "Truck Missed" state for transparency
- Conservative success detection (only when 100% sure)

---

## 🚨 Alert Strategy

### Alert Types
| Type | Trigger | Channels |
|------|---------|----------|
| ✅ SUCCESS | Order confirmed | Telegram + Email |
| ❌ FAILED | Checkout failed | Telegram + Email |
| ⚠️ MISSED | Truck sold out during checkout | Telegram + Email |
| ⚠️ MANUAL | Exceeds auto-buy limit | Telegram + Email |
| 🚨 SYSTEM DOWN | No scan in 5+ minutes | Telegram + Email |
| ✅ SYSTEM RECOVERED | Back online after outage | Telegram |

### Alert Content Format
```
[EMOJI] [TYPE]

[Truck Name]
[Details: Total, Error, etc.]

[Action needed or confirmation]

— E-Comsetter Auto-Buy
```

---

## 💰 Cost Optimization

### History
| Date | Model | Monthly Cost | Change |
|------|-------|--------------|--------|
| 2026-03-15 | Claude Sonnet 4.6 | ~$66/mo | Baseline |
| 2026-03-16 | GPT-4o-mini (backup) | ~$2/mo | -97% |
| 2026-03-20 | Linux cron (primary) | $0 | -100% |
| 2026-03-23 | Xiaomi Mimo V2 Omni | ~$2/mo | Current |

### Current Architecture (Cost: ~$2/month)
- **Primary monitor**: Linux cron + Python (FREE - $0)
- **Backup monitor**: Agent Zero with GPT-4o-mini (~$1.50/mo)
- **6-hour reports**: Agent Zero with GPT-4o-mini (~$0.50/mo)

---

## 📚 Lessons Learned

### What Went Well ✅
1. 3-state detection solved false positive problem
2. Watchdog catches system failures within 15 minutes
3. Telegram bot provides real-time control
4. Cost reduced from $66/mo to $2/mo (97% savings)
5. Screenshots provide irrefutable evidence

### What Could Be Better ⚠️
1. Checkout still takes 60+ seconds (trucks sell out in <2s)
2. Bot crashes are not auto-recovered reliably
3. No way to distinguish "truck never available" vs "truck sold out instantly"
4. Watchdog only checks during business hours

### What We'll Do Differently 🔄
1. Always verify success with real confirmation indicators
2. Always compare timestamps in same timezone
3. Always send alerts before script exits on error
4. Always take screenshots at every step
5. Document bugs immediately when discovered

---

## 🗺️ Future Roadmap

### Phase 1: Reliability (Current)
- [x] 3-state detection
- [x] Watchdog with proper timezone handling
- [x] Telegram Mission Control bot
- [ ] Auto-restart on crash
- [ ] Better error logging

### Phase 2: Speed
- [ ] Reduce checkout time to <30 seconds
- [ ] Pre-login before truck appears
- [ ] Parallel truck checking
- [ ] CDN-based page loading

### Phase 3: Intelligence
- [ ] Predict truck drops based on historical patterns
- [ ] ML model for demand forecasting
- [ ] Dynamic pricing alerts
- [ ] Competition monitoring

### Phase 4: Scale
- [ ] Multi-account support
- [ ] Multiple Good360 locations
- [ ] API-based monitoring (if available)
- [ ] Dashboard UI (Phase 2 Mission Control)

---

## 📞 Quick Reference

### Key Files
| File | Purpose |
|------|--------|
| `/a0/usr/workdir/good360_monitor.py` | Main monitoring script |
| `/a0/usr/workdir/good360_autobuy.py` | Auto-buy with 3-state detection |
| `/a0/usr/workdir/good360_watchdog.py` | Health monitoring |
| `/a0/usr/workdir/good360_telegram_bot.py` | Mission Control bot |
| `/a0/usr/workdir/good360_checkout_config.json` | Credentials and settings |
| `/a0/usr/workdir/good360_paused.flag` | Pause auto-buy |
| `/etc/cron.d/good360` | Cron schedule |

### Key Commands
```bash
# Check system status
pgrep -fa good360_monitor.py
pgrep -fa good360_telegram_bot.py
service cron status

# Restart components
pkill -f good360_monitor.py && nohup python3 good360_monitor.py >> good360_cron.log 2>&1 &
pkill -f good360_telegram_bot.py && sleep 20 && nohup python3 good360_telegram_bot.py > good360_bot.log 2>&1 &

# Pause/Resume auto-buy
touch /a0/usr/workdir/good360_paused.flag  # Pause
rm /a0/usr/workdir/good360_paused.flag      # Resume

# View logs
tail -f /a0/usr/workdir/good360_cron.log
tail -f /a0/usr/workdir/good360_bot.log
```

---

## 📝 Changelog

| Version | Date | Changes |
|---------|------|--------|
| 1.0 | 2026-03-13 | Initial deployment |
| 2.0 | 2026-03-16 | Cost optimization to $2/mo |
| 3.0 | 2026-03-23 | Telegram bot + Watchdog |
| 4.0 | 2026-03-25 | 3-state detection + Bug fixes |

---

*This document should be loaded at the start of each session to provide full context.*
