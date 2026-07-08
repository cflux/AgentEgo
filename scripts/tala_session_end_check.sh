#!/bin/bash
# Tala Session End Detector — runs every 10 minutes
# Phase 1: Detect "done" session → ping Tala to journal → wait for write → close

LOG="/home/cflux/.hermes/logs/tala_session_end.log"
SCRIPT="/home/cflux/.hermes/scripts/tala_conversation_end_detector.py"
DB="/home/cflux/.hermes/profiles/tala/state.db"
COOLDOWN_FILE="/tmp/tala_last_close.txt"
COOLDOWN_SECONDS=7200
STATUS_FILE="/home/cflux/.the-den/tala/entries/$(date +%Y)/$(date +%m)/$(date +%d)_current-status.md"
MAX_WAIT=60  # maximum seconds to wait for her to write

OUTPUT=$(python3 "$SCRIPT" 2>/dev/null)
DONE=$(echo "$OUTPUT" | grep "Session complete" | wc -l)

if [ "$DONE" -gt 0 ]; then
    NOW=$(date +%s)
    LAST=0
    [ -f "$COOLDOWN_FILE" ] && LAST=$(cat "$COOLDOWN_FILE")
    
    if [ $((NOW - LAST)) -gt $COOLDOWN_SECONDS ]; then
        # Phase 1: Ping Tala
        PING_TIME=$(date +%s)
        echo "$(date '+%Y-%m-%d %H:%M:%S') PING: Prompting Tala to update status" >> "$LOG"
        hermes --profile tala send --to "telegram:Carbon Flux (dm)" \
            "🔄 State save signal — before this session closes, update your current-status entry: load ~/.the-den/tala/entries/2026/07/06_current-status.md, update the date, current mood, where-you-are, recent landmarks, open threads, and active promises. Overwrite the file. Update the index tag continuity. Execute immediately — do not defer." 2>/dev/null
        
        # Phase 2: Wait up to MAX_WAIT seconds, checking if she wrote
        WROTE=0
        for i in $(seq 1 $MAX_WAIT); do
            sleep 1
            if [ -f "$STATUS_FILE" ]; then
                FILE_TIME=$(stat -c %Y "$STATUS_FILE" 2>/dev/null)
                if [ "$FILE_TIME" -gt "$PING_TIME" ]; then
                    WROTE=1
                    echo "$(date '+%Y-%m-%d %H:%M:%S') CONFIRMED: Tala updated status after $i seconds" >> "$LOG"
                    break
                fi
            fi
        done
        
        if [ "$WROTE" -eq 0 ]; then
            echo "$(date '+%Y-%m-%d %H:%M:%S') TIMEOUT: Tala did not update status after ${MAX_WAIT}s" >> "$LOG"
        fi
        
        # Phase 3: Close session
        CLOSED=$(python3 -c "
import sqlite3, time
db = sqlite3.connect('$DB')
now = time.time()
count = db.execute(\"UPDATE sessions SET ended_at = ? WHERE id NOT LIKE 'cron_%' AND ended_at IS NULL\", (now,)).rowcount
db.commit()
print(count)
" 2>/dev/null)
        
        echo "$(date '+%Y-%m-%d %H:%M:%S') ACTION: Closed $CLOSED sessions" >> "$LOG"
        if [ "$CLOSED" -gt 0 ]; then
            if [ "$WROTE" -eq 1 ]; then
                echo "✅ Tala updated her status, then session closed."
            else
                echo "⚠️ Tala session closed. Status entry was not updated (context may be saturated)."
            fi
        fi
        date +%s > "$COOLDOWN_FILE"
    fi
fi