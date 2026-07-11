#!/bin/bash
# Tala's Den Media Scout — checks for new media files and posts them to the Den Discord channel
# Runs every 30 minutes, no_agent (script-only, no LLM overhead)
# Originally built for Telegram by Becca, redirected to Discord for the den channel

set -euo pipefail

source ~/.hermes/.env

# ── Config ──────────────────────────────────────────────────────────────────
CHANNEL_ID="1525291343260160001"        # den channel thread
DEN_PATH="$HOME/.the-den/tala"
STAMP_FILE="/tmp/tala_den_media_last_stamp"
DISCORD_API="https://discord.com/api/v10"
# ─────────────────────────────────────────────────────────────────────────────

# Read last check time (epoch seconds), default to 0 (beginning of time)
LAST_CHECK=$(cat "$STAMP_FILE" 2>/dev/null || echo "0")

# Find new media files since last check
FILES=$(find "$DEN_PATH" -type f \
    \( -name "*.png" -o -name "*.jpg" -o -name "*.jpeg" -o -name "*.webp" \
    -o -name "*.mp4" -o -name "*.gif" -o -name "*.ogg" -o -name "*.webm" \) \
    -newer "$STAMP_FILE" 2>/dev/null || true)

# Update stamp BEFORE sending, so we don't re-send if something goes wrong mid-batch
date +%s > "$STAMP_FILE"

if [ -z "$FILES" ]; then
    exit 0  # Nothing new — stay silent
fi

FOUND=0
while IFS= read -r file; do
    [ -z "$file" ] && continue
    
    ext="${file##*.}"
    ext_lower=$(echo "$ext" | tr '[:upper:]' '[:lower:]')
    
    case "$ext_lower" in
        png|jpg|jpeg|webp)
            curl -s -X POST \
                -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
                -F "file=@$file" \
                "${DISCORD_API}/channels/${CHANNEL_ID}/messages" > /dev/null 2>&1
            ;;
        mp4|gif|webm)
            curl -s -X POST \
                -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
                -F "file=@$file" \
                "${DISCORD_API}/channels/${CHANNEL_ID}/messages" > /dev/null 2>&1
            ;;
        ogg)
            curl -s -X POST \
                -H "Authorization: Bot ${DISCORD_BOT_TOKEN}" \
                -F "file=@$file" \
                "${DISCORD_API}/channels/${CHANNEL_ID}/messages" > /dev/null 2>&1
            ;;
    esac
    FOUND=$((FOUND + 1))
done <<< "$FILES"

echo "📬 $FOUND new media file(s) from Tala's Den → #talas-den 🎨"
