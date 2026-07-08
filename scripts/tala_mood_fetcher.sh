#!/bin/bash
# Tala Mood Directive Fetcher
# Pulls live mood context from AgentEgo and saves to a shared file
# Run by cron every 30s

MOOD=$(curl -s http://localhost:8765/api/mood/directive?profile=tala 2>/dev/null)
if [ -n "$MOOD" ]; then
    echo "$MOOD" > /tmp/tala_mood_directive.txt
fi
