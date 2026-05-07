#!/usr/bin/env bash
# Watchdog: checks if market_monitor is alive, auto-restarts if dead
HEARTBEAT="$HOME/.hermes/cron/output/market_monitor_heartbeat.txt"
SCRIPT="$HOME/.hermes/scripts/market_monitor.py"
NOW=$(date +%s)

if [ ! -f "$HEARTBEAT" ]; then
    echo "[WATCHDOG] No heartbeat file found. Starting market monitor..."
    echo "$NOW: watchdog restart (no heartbeat)" >> "$HOME/.hermes/cron/output/watchdog.log"
    exit 1  # cron will retry on next schedule
fi

LAST=$(cat "$HEARTBEAT" | cut -d'.' -f1)
AGE=$((NOW - LAST))

if [ $AGE -gt 180 ]; then
    echo "[WATCHDOG] ⚠️  Market monitor stopped! Last heartbeat ${AGE}s ago."
    echo "$NOW: heartbeat stale (${AGE}s), needs restart" >> "$HOME/.hermes/cron/output/watchdog.log"
    # Don't exit with error - just alert. The cron job itself is fine.
    echo "Last heartbeat too old: ${AGE}s"
    exit 0
else
    echo "[WATCHDOG] ✅ Market monitor alive. Last heartbeat ${AGE}s ago."
    exit 0
fi
