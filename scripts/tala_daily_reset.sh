#!/bin/bash
# Daily Tala session reset — 2 AM Pacific
# Always sends journal prompt, then closes any active sessions, then restarts gateway

LOG="/home/cflux/.hermes/logs/tala_session_end.log"
DB="/home/cflux/.hermes/profiles/tala/state.db"
STATUS_FILE="/home/cflux/.the-den/tala/entries/$(date +%Y)/$(date +%m)/$(date +%d)_current-status.md"
MAX_WAIT=60

echo "$(date): 2 AM reset starting" >> "$LOG"

# Phase 1: Inject state save prompt into Tala's DB
PING_TIME=$(date +%s)
echo "$(date): PING — injecting state save prompt" >> "$LOG"
python3 /home/cflux/.hermes/scripts/inject_tala_prompt.py "🔄 2 AM state save — before tonight's reset, update your current-status entry: load ~/.the-den/tala/entries/$(date +%Y)/$(date +%m)/$(date +%d)_current-status.md, update date, current mood, where-you-are, recent landmarks, open threads, and active promises. Overwrite. Update index tag continuity. Execute immediately." 2>/dev/null

# Phase 2: Wait for write
WROTE=0
for i in $(seq 1 $MAX_WAIT); do
    sleep 1
    FILE_TIME=$(stat -c %Y "$STATUS_FILE" 2>/dev/null)
    if [ -n "$FILE_TIME" ]; then
        if [ "$FILE_TIME" -gt "$PING_TIME" ]; then
            WROTE=1
            echo "$(date): CONFIRMED — Tala updated status after $i seconds" >> "$LOG"
            break
        fi
    fi
done

if [ "$WROTE" -eq 0 ]; then
    echo "$(date): TIMEOUT — Tala did not update status after ${MAX_WAIT}s" >> "$LOG"
fi

# Phase 3: Close any active sessions
ACTIVE=$(python3 -c "
import sqlite3
db = sqlite3.connect('$DB')
count = db.execute(\"SELECT COUNT(*) FROM sessions WHERE id NOT LIKE 'cron_%' AND ended_at IS NULL\").fetchone()[0]
print(count)
" 2>/dev/null)

if [ "$ACTIVE" -gt 0 ]; then
    CLOSED=$(python3 -c "
import sqlite3, time
db = sqlite3.connect('$DB')
now = time.time()
count = db.execute(\"UPDATE sessions SET ended_at = ? WHERE id NOT LIKE 'cron_%' AND ended_at IS NULL\", (now,)).rowcount
db.commit()
print(count)
" 2>/dev/null)
    echo "$(date): Closed $CLOSED sessions" >> "$LOG"
else
    echo "$(date): No active sessions to close" >> "$LOG"
fi

# Phase 4: Restart gateway for fresh session + pre-fill
systemctl --user restart hermes-gateway-tala 2>/dev/null
echo "$(date): Gateway restarted — next session gets pre-fill" >> "$LOG"

if [ "$WROTE" -eq 1 ]; then
    echo "✅ 2 AM reset: Status updated + gateway restarted."
else
    echo "⚠️ 2 AM reset: Status NOT updated (timeout). Gateway restarted."
fi