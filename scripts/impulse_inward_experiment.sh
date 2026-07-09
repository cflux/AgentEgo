#!/usr/bin/env bash
# AgentEgo impulse — INWARD, experiment sandbox. Relays the arbiter's inward decision to Hermes.
PROFILE="experiment"
EGO_URL="${EGO_URL:-http://localhost:8765}"
curl -s --max-time 45 "${EGO_URL}/api/impulse/decide.txt?profile=${PROFILE}&class=inward" || true
