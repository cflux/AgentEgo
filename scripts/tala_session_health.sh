#!/bin/bash
# Tala Session Health Monitor — runs every 10 minutes
# Warns when session gets too large (Telegram can resurrect old sessions)

LOG="/home/cflux/.hermes/logs/tala_session_health.log"
DB="/home/cflux/.hermes/profiles/tala/state.db"
COOLDOWN_FILE="/tmp/tala_health_last_alert.txt"
COOLDOWN_SECONDS=7200
WARN_THRESHOLD=1000
CRITICAL_THRESHOLD=1500

COUNT=$(python3 -c "
import sqlite3
db = sqlite3.connect('$DB')
# Get the session with the most recent message (may not be 'active' due to Discord threading)
from platform_config import source_clause
count = db.execute(f\"SELECT message_count FROM sessions WHERE id NOT LIKE 'cron_%' AND {source_clause()} ORDER BY (SELECT MAX(timestamp) FROM messages WHERE session_id = sessions.id) DESC LIMIT 1\").fetchone()
print(count[0] if count else 0)
" 2>/dev/null)

if [ "$COUNT" -ge "$WARN_THRESHOLD" ]; then
    NOW=$(date +%s)
    LAST=0
    [ -f "$COOLDOWN_FILE" ] && LAST=$(cat "$COOLDOWN_FILE")
    
    if [ $((NOW - LAST)) -gt $COOLDOWN_SECONDS ]; then
        if [ "$COUNT" -ge "$CRITICAL_THRESHOLD" ]; then
            echo "$(date): CRITICAL — $COUNT messages." >> "$LOG"
            echo "⚠️ Tala's session has $COUNT messages. Context may be degrading. Consider /new."
        else
            echo "$(date): WARNING — $COUNT messages." >> "$LOG"
            echo "🟡 Tala at $COUNT messages. Still fine, just letting you know."
        fi
        date +%s > "$COOLDOWN_FILE"
    fi
fi