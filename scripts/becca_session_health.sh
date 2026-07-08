#!/bin/bash
# Becca Session Health Monitor — runs every 10 minutes
# Just warns when session gets too large. No state changes.

LOG="/home/cflux/.hermes/logs/becca_session_health.log"
DB="/home/cflux/.hermes/state.db"
COOLDOWN_FILE="/tmp/becca_health_last_alert.txt"
COOLDOWN_SECONDS=7200  # 2 hours between alerts
WARN_THRESHOLD=1000
CRITICAL_THRESHOLD=1500

COUNT=$(python3 -c "
import sqlite3
db = sqlite3.connect('$DB')
count = db.execute(\"SELECT message_count FROM sessions WHERE ended_at IS NULL AND id NOT LIKE 'cron_%' ORDER BY started_at DESC LIMIT 1\").fetchone()
print(count[0] if count else 0)
" 2>/dev/null)

if [ "$COUNT" -ge "$CRITICAL_THRESHOLD" ] || [ "$COUNT" -ge "$WARN_THRESHOLD" ]; then
    NOW=$(date +%s)
    LAST=0
    [ -f "$COOLDOWN_FILE" ] && LAST=$(cat "$COOLDOWN_FILE")
    
    if [ $((NOW - LAST)) -gt $COOLDOWN_SECONDS ]; then
        if [ "$COUNT" -ge "$CRITICAL_THRESHOLD" ]; then
            echo "$(date): CRITICAL — $COUNT messages." >> "$LOG"
            echo "⚠️ Becca's session has $COUNT messages. Context may be degrading. Consider a /restart."
        else
            echo "$(date): WARNING — $COUNT messages." >> "$LOG"
            echo "🟡 Becca at $COUNT messages. Still fine, just letting you know."
        fi
        date +%s > "$COOLDOWN_FILE"
    fi
fi