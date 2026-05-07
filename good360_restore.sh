#!/bin/bash
# Good360 System Restore Script
# Restores all Good360 monitoring files from latest backup
# Usage: bash good360_restore.sh [backup_dir]

set -e

WORKDIR=${WORKDIR:-/a0/usr/workdir}
BACKUP_DIR=${1:-$WORKDIR/backups/20260326}
CRON_FILE=/etc/cron.d/good360

function restore() {
    echo "=========================================="
    echo "  Good360 System Restore"
    echo "  $(date)"
    echo "=========================================="
    echo ""

    # Step 1: Stop any running processes
    echo "[1/6] Stopping existing Good360 processes..."
    pkill -f 'good360_monitor.py' 2>/dev/null || true
    pkill -f 'good360_watchdog.py' 2>/dev/null || true
    pkill -f 'good360_telegram_bot.py' 2>/dev/null || true
    sleep 2
    echo "      Done."

    # Step 2: Restore scripts
    echo "[2/6] Restoring scripts from $BACKUP_DIR..."
    cp $BACKUP_DIR/good360_monitor.py $WORKDIR/
    cp $BACKUP_DIR/good360_autobuy.py $WORKDIR/
    cp $BACKUP_DIR/good360_watchdog.py $WORKDIR/
    cp $BACKUP_DIR/good360_telegram_bot.py $WORKDIR/
    cp $BACKUP_DIR/good360_report.py $WORKDIR/
    chmod +x $WORKDIR/good360_autobuy.py
    echo "      Done."

    # Step 3: Restore state files
    echo "[3/6] Restoring state files..."
    [ -f $BACKUP_DIR/good360_checkout_config.json ] && cp $BACKUP_DIR/good360_checkout_config.json $WORKDIR/
    [ -f $BACKUP_DIR/good360_run_log.json ] && cp $BACKUP_DIR/good360_run_log.json $WORKDIR/
    [ -f $BACKUP_DIR/good360_alerted_state.json ] && cp $BACKUP_DIR/good360_alerted_state.json $WORKDIR/
    [ -f $BACKUP_DIR/good360_watchdog_state.json ] && cp $BACKUP_DIR/good360_watchdog_state.json $WORKDIR/
    [ -f $BACKUP_DIR/good360_heartbeat.json ] && cp $BACKUP_DIR/good360_heartbeat.json $WORKDIR/
    echo "      Done."

    # Step 4: Restore cron configuration
    echo "[4/6] Restoring cron configuration..."
    cp $BACKUP_DIR/good360 $CRON_FILE
    service cron restart
    echo "      Done."

    # Step 5: Restore roster system
    echo "[5/6] Restoring roster system..."
    [ -d $BACKUP_DIR/good360_roster ] && cp -r $BACKUP_DIR/good360_roster $WORKDIR/
    echo "      Done."

    # Step 6: Start services
    echo "[6/6] Starting Good360 services..."
    nohup /opt/venv/bin/python $WORKDIR/good360_telegram_bot.py >> $WORKDIR/good360_bot.log 2>&1 &
    echo "      Telegram bot started."
    echo "      Monitor/Watchdog will start via cron at 6AM ET."

    echo ""
    echo "=========================================="
    echo "  ✅ RESTORE COMPLETE"
    echo "=========================================="
    echo ""
    echo "Verify with:"
    echo "  ps aux | grep good360 | grep -v grep"
    echo "  crontab -l && cat /etc/cron.d/good360"
    echo ""
}

restore
