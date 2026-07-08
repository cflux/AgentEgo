#!/bin/bash
# Becca Mood Directive Fetcher
# Pulls live mood context from AgentEgo for the default profile
# Run by cron every 60s

MOOD=$(curl -s http://localhost:8765/api/mood/directive?profile=default 2>/dev/null)
if [ -n "$MOOD" ]; then
    echo "$MOOD" > /tmp/becca_mood_directive.txt
fi
