#!/bin/bash
# Tala Dream & Wake Fetcher — Daily Reflection Pipeline
# Runs once per day (cron at 4 AM). Saves full content to both temp files AND log.

LOG_FILE="/home/cflux/.hermes/logs/tala_reflection.log"
FULL_LOG_DIR="/home/cflux/.hermes/logs/reflections"
mkdir -p "$FULL_LOG_DIR"
DATE=$(date '+%Y-%m-%d %H:%M:%S')

echo "=== $DATE ===" >> "$LOG_FILE"

# Dream (consumed on read — one-shot)
DREAM=$(curl -s "http://localhost:8765/api/reflection/dream.txt?profile=tala" 2>/dev/null)
if [ -n "$DREAM" ]; then
    echo "$DREAM" > /tmp/tala_dream.txt
    echo "$DREAM" > "$FULL_LOG_DIR/dream_latest.txt"
    echo "DREAM: found ($(echo "$DREAM" | wc -c) bytes)" >> "$LOG_FILE"
else
    echo "DREAM: none (empty or already consumed)" >> "$LOG_FILE"
fi

# Wake (consumed on read — mood reset + conclusions)
WAKE=$(curl -s "http://localhost:8765/api/reflection/wake.txt?profile=tala" 2>/dev/null)
if [ -n "$WAKE" ]; then
    echo "$WAKE" > /tmp/tala_wake.txt
    echo "$WAKE" > "$FULL_LOG_DIR/wake_latest.txt"
    echo "WAKE:  found ($(echo "$WAKE" | wc -c) bytes)" >> "$LOG_FILE"
else
    echo "WAKE:  none (empty or already consumed)" >> "$LOG_FILE"
fi

echo "" >> "$LOG_FILE"
echo "Reflection fetcher completed — full content at $FULL_LOG_DIR/"
