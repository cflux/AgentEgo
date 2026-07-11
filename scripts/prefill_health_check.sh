#!/bin/bash
# Prefill Health Check — catches silent injection failures
# Runs daily. Alerts if no successful prefill injection in 48+ hours.
# Writes to health_alerts.log for CLI-mode diagnostics.

ALERT_LOG="/home/cflux/.hermes/logs/health_alerts.log"
PREFILL_LOG="/home/cflux/.hermes/logs/prefill_injection.log"
MAX_AGE_HOURS=48
NOW=$(date +%s)

if [ ! -f "$PREFILL_LOG" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') PREFILL CHECK: Log file missing — prefill injector may not be firing." >> "$ALERT_LOG"
    exit 0
fi

# Get last modification time of prefill log
LOG_MTIME=$(stat -c %Y "$PREFILL_LOG" 2>/dev/null)
if [ -z "$LOG_MTIME" ]; then
    exit 0
fi

AGE_SECONDS=$((NOW - LOG_MTIME))
AGE_HOURS=$((AGE_SECONDS / 3600))

if [ "$AGE_HOURS" -ge "$MAX_AGE_HOURS" ]; then
    echo "$(date '+%Y-%m-%d %H:%M:%S') ⚠️  PREFILL STALE: Last injection was ${AGE_HOURS}h ago (threshold: ${MAX_AGE_HOURS}h). Prefill injector may be silently failing." >> "$ALERT_LOG"
fi
