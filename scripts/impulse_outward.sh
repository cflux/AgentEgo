#!/usr/bin/env bash
# AgentEgo impulse check-in — OUTWARD class (Hermes cron pre-run script).
#
# Relays the arbiter's outward decision to Hermes. On a fire, the composed message-intent prompt is
# printed (carrying the [IMPULSE-OUTWARD] marker); the agent composes a DM, which the agentego-impulse
# plugin delivers to the user's DM and mirrors into the live transcript. On "nothing" the output is
# empty, so Hermes skips the agent entirely.
#
# Install: copy to the script dir the profile's cron resolves, then:
#   hermes -p tala cron create "2h" \
#     "The note above is a thought you decided to reach out to the user about. Say it to them naturally, \
#      in your own voice. If there is no note above, do nothing." \
#     --name impulse-outward --script impulse_outward.sh --deliver local

PROFILE="${EGO_PROFILE:-tala}"
EGO_URL="${EGO_URL:-http://localhost:8765}"

curl -s --max-time 45 "${EGO_URL}/api/impulse/decide.txt?profile=${PROFILE}&class=outward" || true
