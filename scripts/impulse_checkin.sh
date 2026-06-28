#!/usr/bin/env bash
# AgentEgo impulse check-in (Hermes cron pre-run script).
#
# Relays AgentEgo's impulse decision to Hermes. If an impulse fires, the built
# prompt is printed (and injected into the agent prompt). If not, output is empty,
# which makes Hermes skip the agent entirely — no LLM call, no delivery.
#
# Install: copy to ~/.hermes/scripts/ and create a cron job, e.g.:
#   hermes cron create "30m" \
#     "The text above is a self-directed impulse you decided to act on while idle. Carry it out fully using your tools, then share the result with the user in your own voice and character. If there is no instruction above, do nothing." \
#     --name agentego-impulse-default --script impulse_checkin.sh --deliver origin
#
# Usage: impulse_checkin.sh [profile]   (defaults to "default")

PROFILE="${1:-default}"
EGO_URL="${EGO_URL:-http://localhost:8765}"

curl -s --max-time 20 "${EGO_URL}/api/impulse/next.txt?profile=${PROFILE}" || true
