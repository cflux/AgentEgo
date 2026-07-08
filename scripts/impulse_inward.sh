#!/usr/bin/env bash
# AgentEgo impulse check-in — INWARD class (Hermes cron pre-run script).
#
# Relays the arbiter's inward decision to Hermes. On a fire, the composed impulse prompt is printed
# (and injected into the agent prompt); the turn runs silently and persists via the Den. On "nothing"
# the output is empty, so Hermes skips the agent entirely (no LLM call, no delivery).
#
# Install: copy to the script dir the profile's cron resolves, then:
#   hermes -p tala cron create "30m" \
#     "The note above is something you decided to do on your own while idle. Carry it out fully with \
#      your tools, then record it in your Den. If there is no note above, do nothing." \
#     --name impulse-inward --script impulse_inward.sh --deliver local \
#     --skill research --skill reddit-browsing --skill creative/comfyui --skill creative/local-image-gen
#
# Attach only skills for capabilities currently enabled in the manifest.

PROFILE="${EGO_PROFILE:-tala}"
EGO_URL="${EGO_URL:-http://localhost:8765}"

curl -s --max-time 45 "${EGO_URL}/api/impulse/decide.txt?profile=${PROFILE}&class=inward" || true
