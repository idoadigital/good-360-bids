#!/bin/bash
# ============================================================
# Good360 Startup Restore Script
# Created: 2026-04-13 — Permanent fix for dependency loss
# Run automatically @reboot via /etc/cron.d/good360
# ============================================================

set -e
WORKDIR=${WORKDIR:-/a0/usr/workdir}
LOG=$WORKDIR/startup_restore.log
VENV=/opt/venv/bin/python
PIP=/opt/venv/bin/pip

echo "[$(date)] === startup_restore.sh starting ===" >> $LOG

# STEP 1: Install all required Python packages
echo "[$(date)] Installing Python dependencies..." >> $LOG
$PIP install --quiet \
    playwright \
    pytz \
    python-telegram-bot \
    requests \
    python-dotenv \
    httpx \
    aiohttp >> $LOG 2>&1

# STEP 2: Ensure Playwright Chromium browser is installed
echo "[$(date)] Ensuring Playwright Chromium is installed..." >> $LOG
$VENV -m playwright install chromium >> $LOG 2>&1 || true

# STEP 3: Restore /etc/cron.d/good360 from backup if missing
if [ ! -f /etc/cron.d/good360 ]; then
    echo "[$(date)] WARNING: /etc/cron.d/good360 missing! Restoring from backup..." >> $LOG
    if [ -f $WORKDIR/backups/20260326/good360 ]; then
        cp $WORKDIR/backups/20260326/good360 /etc/cron.d/good360
        chmod 644 /etc/cron.d/good360
        echo "[$(date)] Cron config restored from 20260326 backup." >> $LOG
    else
        echo "[$(date)] ERROR: Backup cron config not found! Manual intervention required." >> $LOG
    fi
else
    echo "[$(date)] /etc/cron.d/good360 exists — OK" >> $LOG
fi

# STEP 4: Restart cron service
echo "[$(date)] Restarting cron service..." >> $LOG
service cron restart >> $LOG 2>&1

# STEP 5: Kill any stale bot instances
echo "[$(date)] Clearing stale bot processes..." >> $LOG
pkill -9 -f good360_telegram_bot.py 2>/dev/null || true
sleep 3

# STEP 6: Start Telegram bot
echo "[$(date)] Starting Telegram bot..." >> $LOG
nohup $VENV $WORKDIR/good360_telegram_bot.py >> $WORKDIR/good360_bot.log 2>&1 &
echo $! > $WORKDIR/good360_bot.pid
echo "[$(date)] Bot started with PID $(cat $WORKDIR/good360_bot.pid)" >> $LOG

# STEP 7: Start roster orchestrator in daemon mode
echo "[$(date)] Starting roster orchestrator..." >> $LOG
pkill -9 -f roster_orchestrator.py 2>/dev/null || true
sleep 2
nohup $VENV $WORKDIR/good360_roster/roster_orchestrator.py daemon >> $WORKDIR/good360_roster/orchestrator.log 2>&1 &
echo "[$(date)] Orchestrator started." >> $LOG

echo "[$(date)] === startup_restore.sh COMPLETE ===" >> $LOG
