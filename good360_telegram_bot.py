#!/usr/bin/env python3
"""
E-Comsetter Good360 Mission Control — Telegram Command Bot
Commands: /status /logs /test /pause /resume /pending /approve /reject /help
"""

import json
import os
import subprocess
from datetime import datetime

import pytz
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# === CONFIG ===
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_CHAT_IDS = [
    int(x) for x in [
        os.environ.get("TELEGRAM_GROUP_HOPE4HUMANITY"),
        os.environ.get("TELEGRAM_GROUP_REVIVING_HOMES"),
        os.environ.get("TELEGRAM_OPERATOR_CHAT_ID"),
    ] if x
]
CRON_LOG = f"{os.environ.get('WORKDIR', '/a0/usr/workdir')}/good360_cron.log"

# Org-specific group mapping
ORG_GROUPS = {
    -1003866351492: "hope4humanity",
    -5089630277: "reviving_homes"
}

def get_org_for_chat(chat_id):
    return ORG_GROUPS.get(chat_id, None)

_WORKDIR = os.environ.get("WORKDIR", "/a0/usr/workdir")

def get_pause_file(org_name):
    return f"{_WORKDIR}/good360_paused_{org_name}.flag"

def get_org_status(org_name):
    """Read actual org status from monitor's shared status file"""
    import json
    status_file = f"{_WORKDIR}/good360_org_status_{org_name}.json"
    try:
        if os.path.exists(status_file):
            with open(status_file) as f:
                return json.load(f)
    except:
        pass
    return None
RUN_LOG = f"{_WORKDIR}/good360_run_log.json"
ALERTED_STATE = f"{_WORKDIR}/good360_alerted_state.json"
MONITOR_SCRIPT = f"{_WORKDIR}/good360_monitor.py"
PAUSE_FILE = f"{_WORKDIR}/good360_paused.flag"
REVIEW_QUEUE = f"{_WORKDIR}/intake_form/review_queue.json"
FORM_DATA_DIR = f"{_WORKDIR}/intake_form/submissions"
ET = pytz.timezone("America/New_York")

def is_authorized(chat_id):
    return chat_id in ALLOWED_CHAT_IDS

def is_paused(org_name=None):
    if org_name:
        return os.path.exists(get_pause_file(org_name))
    # Legacy: check global pause
    return os.path.exists(PAUSE_FILE)

def get_last_run_time():
    try:
        with open(CRON_LOG) as f:
            lines = f.readlines()
        for line in reversed(lines):
            if 'Checking Good360' in line:
                ts = line.strip()[1:20]
                return ts
    except:
        pass
    return "Unknown"

def get_recent_logs(n=60):
    """Get recent runs - focused on tracked trucks only"""
    try:
        with open(RUN_LOG) as f:
            data = json.load(f)
        runs = data.get("runs", [])[-n:]
        result = []
        for run in reversed(runs):
            time_str = run.get("time", "")[11:]
            trucks = run.get("trucks", [])
            alert_sent = run.get("alert_sent", False)
            action = run.get("action", "")
            tracked_avail = [t for t in trucks if t.get("tracked") and t.get("available")]
            if tracked_avail:
                names = [t["name"].replace(" Truckload", "").replace(" Amazon", "").replace(" - Maysville, KY", "") for t in tracked_avail]
                status = chr(10067) + " " + ", ".join(names)
            elif action:
                status = chr(128722) + " " + action
            else:
                status = chr(9989) + " clear"
            result.append(time_str + " | " + status)
        return chr(10).join(result) if result else "No runs logged yet"
    except Exception as e:
        return f"Error reading logs: {e}"

def get_cron_status():
    try:
        result = subprocess.run(['service', 'cron', 'status'], capture_output=True, text=True)
        return 'running' in result.stdout.lower()
    except:
        return False

def load_review_queue():
    try:
        with open(REVIEW_QUEUE) as f:
            return json.load(f)
    except:
        return {'pending': [], 'approved': [], 'rejected': []}

def save_review_queue(queue):
    with open(REVIEW_QUEUE, 'w') as f:
        json.dump(queue, f, indent=2)

# === COMMANDS ===

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎛️ *E-Comsetter Mission Control*\n\n"
        "*System Commands:*\n"
        "/status — System health & last run\n"
        "/logs — Last 60 monitoring runs\n"
        "/test — Trigger immediate scan now\n"
        "/pause — Pause auto-buy (alerts still fire)\n"
        "/resume — Resume auto-buy\n"
        "/help — Show this menu\n\n"
        "*Intake Form Commands:*\n"
        "/pending — List pending intake forms\n"
        "/approve <REF> — Approve an intake form\n"
        "/reject <REF> — Reject an intake form\n"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    cron_ok = get_cron_status()
    last_run = get_last_run_time()
    org_name = get_org_for_chat(update.effective_chat.id)

    # Read ACTUAL status from monitor's shared file
    org_status = get_org_status(org_name) if org_name else None

    if org_status:
        paused = org_status.get('paused', False)
        cooldown_active = org_status.get('cooldown_active', False)
        cooldown_until = org_status.get('cooldown_until', '')
        auto_buy = org_status.get('auto_buy_active', not paused and not cooldown_active)
    else:
        paused = is_paused(org_name)
        cooldown_active = False
        cooldown_until = ''
        auto_buy = not paused

    # Show correct status icon
    if cooldown_active:
        status_icon = "⏸️"
    elif paused:
        status_icon = "⏸️"
    else:
        status_icon = "▶️"
    monitor_icon = "✅" if cron_ok else "🔴"

    org_display = org_name.replace('_', ' ').title() if org_name else 'Unknown'
    text = (
        f"{status_icon} *E-Comsetter Monitor — {org_display}*\n\n"
        f"{monitor_icon} Monitor: {'Running' if cron_ok else 'DOWN!'}\n"
        f"⏯️ Auto-Buy: {'⏸️ PAUSED' if paused else '▶️ ACTIVE'}\n"
        f"🕐 Last Scan: {last_run}\n\n"
        f"🎯 *Targets:*\n"
        f"• New Unsorted\n"
        f"• Variety\n"
        f"• Assorted Houseware\n\n"
        f"⏱️ Scanning every 1 min | Mon–Fri 6AM–11PM ET"
    )
    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_logs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    logs = get_recent_logs(60)
    text = f"📋 *Last 60 Scans*\n`{logs}`"
    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    await update.message.reply_text("⚡ Triggering immediate scan...")
    try:
        result = subprocess.run(
            ['/opt/venv/bin/python', MONITOR_SCRIPT],
            capture_output=True, text=True, timeout=60
        )
        output = result.stdout[-800:] if result.stdout else "No output"
        await update.message.reply_text(f"✅ *Scan complete:*\n`{output}`", parse_mode='Markdown')
    except Exception as e:
        await update.message.reply_text(f"❌ Error: {e}")

async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    with open(PAUSE_FILE, 'w') as f:
        f.write(f"Paused at {datetime.now()}")
    await update.message.reply_text(
        "⏸️ *Auto-buy PAUSED*\nMonitoring continues. No purchases will be made.\nSend /resume to re-activate.",
        parse_mode='Markdown'
    )

async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return
    if os.path.exists(PAUSE_FILE):
        os.remove(PAUSE_FILE)
        await update.message.reply_text("🟢 *Auto-buy RESUMED*", parse_mode='Markdown')
    else:
        await update.message.reply_text("ℹ️ Auto-buy was already active!")

async def cmd_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    queue = load_review_queue()
    pending = queue.get('pending', [])

    if not pending:
        await update.message.reply_text("✅ No pending intake forms.")
        return

    text = f"📋 *Pending Intake Forms ({len(pending)}):*\n\n"
    for i, item in enumerate(pending, 1):
        ref = item.get('reference', 'N/A')
        org = item.get('org_name', 'Unknown')
        submitted = item.get('submitted', 'Unknown')[:16]
        text += f"{i}. *{org}*\n"
        text += f"   Ref: `{ref}`\n"
        text += f"   Submitted: {submitted}\n"
        text += f"   /approve {ref} | /reject {ref}\n\n"

    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_approve(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /approve <REFERENCE>\nExample: /approve EC-M1K3XYZ")
        return

    ref = context.args[0].upper()
    queue = load_review_queue()

    # Find the pending item
    pending = queue.get('pending', [])
    item = next((p for p in pending if p.get('reference', '').upper() == ref), None)

    if not item:
        await update.message.reply_text(f"❌ No pending form found with reference `{ref}`")
        return

    # Load submission data
    sub_file = item.get('submission_file')
    if not sub_file or not os.path.exists(sub_file):
        await update.message.reply_text(f"❌ Submission file not found: {sub_file}")
        return

    with open(sub_file) as f:
        data = json.load(f)

    # Move from pending to approved
    queue['pending'] = [p for p in pending if p.get('reference', '').upper() != ref]
    item['approved_at'] = datetime.now().isoformat()
    item['approved_by'] = 'telegram'
    queue['approved'].append(item)
    save_review_queue(queue)

    # Send approval confirmation with org details
    org_name = data.get('org_legal_name', 'Unknown')
    max_price = data.get('max_price', '6400')
    truck_types = data.get('truck_types', [])
    email = data.get('contact_email', 'N/A')
    phone = data.get('contact_phone', 'N/A')

    text = f"""✅ *INTAKE FORM APPROVED!*

🏢 *Organization:* {org_name}
📝 *Reference:* `{ref}`
📧 *Email:* {email}
📞 *Phone:* {phone}
💰 *Max Price:* ${max_price}
🚛 *Truck Types:* {', '.join(truck_types)}

*Next Steps:*
1. Create a Telegram group for {org_name}
2. Add the bot to the group
3. Send me the group ID with:
   /setgroup {ref} <group_id>

The org will be activated once you set the group ID."""

    await update.message.reply_text(text, parse_mode='Markdown')

async def cmd_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if not context.args:
        await update.message.reply_text("Usage: /reject <REFERENCE>\nExample: /reject EC-M1K3XYZ")
        return

    ref = context.args[0].upper()
    queue = load_review_queue()

    # Find the pending item
    pending = queue.get('pending', [])
    item = next((p for p in pending if p.get('reference', '').upper() == ref), None)

    if not item:
        await update.message.reply_text(f"❌ No pending form found with reference `{ref}`")
        return

    # Move from pending to rejected
    queue['pending'] = [p for p in pending if p.get('reference', '').upper() != ref]
    item['rejected_at'] = datetime.now().isoformat()
    item['rejected_by'] = 'telegram'
    queue['rejected'].append(item)
    save_review_queue(queue)

    org_name = item.get('org_name', 'Unknown')
    await update.message.reply_text(
        f"❌ *Intake Form Rejected*\n\n🏢 Organization: {org_name}\n📝 Reference: `{ref}`\n\nThe form has been removed from the pending queue.",
        parse_mode='Markdown'
    )

async def cmd_setgroup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_chat.id):
        await update.message.reply_text("⛔ Unauthorized.")
        return

    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setgroup <REFERENCE> <GROUP_ID>\nExample: /setgroup EC-M1K3XYZ -1001234567890")
        return

    ref = context.args[0].upper()
    group_id = context.args[1]

    queue = load_review_queue()

    # Find the approved item
    approved = queue.get('approved', [])
    item = next((a for a in approved if a.get('reference', '').upper() == ref), None)

    if not item:
        await update.message.reply_text(f"❌ No approved form found with reference `{ref}`. Approve it first with /approve {ref}")
        return

    # Update the item with group ID
    item['telegram_group_id'] = group_id
    item['activated_at'] = datetime.now().isoformat()
    save_review_queue(queue)

    org_name = item.get('org_name', 'Unknown')

    await update.message.reply_text(
        f"✅ *Organization Activated!*\n\n🏢 *{org_name}*\n📝 Reference: `{ref}`\n👥 Telegram Group: `{group_id}`\n\n*System is now monitoring for truckloads!*\nAlerts will be sent to this group.",
        parse_mode='Markdown'
    )

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("start", cmd_help))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("logs", cmd_logs))
    app.add_handler(CommandHandler("test", cmd_test))
    app.add_handler(CommandHandler("pause", cmd_pause))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("pending", cmd_pending))
    app.add_handler(CommandHandler("approve", cmd_approve))
    app.add_handler(CommandHandler("reject", cmd_reject))
    app.add_handler(CommandHandler("setgroup", cmd_setgroup))
    print(f"[{datetime.now()}] E-Comsetter Mission Control Bot started...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
