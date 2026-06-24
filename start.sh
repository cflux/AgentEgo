#!/usr/bin/env bash
# Run AgentEgo directly (without Docker).
# Usage: ./start.sh [--port 8765]

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ ! -d .venv ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi

mkdir -p data

export EGO_HERMES_DB_PATH="${EGO_HERMES_DB_PATH:-/home/cflux/.hermes/state.db}"
export EGO_EGO_DB_PATH="${EGO_EGO_DB_PATH:-$SCRIPT_DIR/data/ego.db}"
export EGO_RETENTION_DAYS="${EGO_RETENTION_DAYS:-7}"

exec .venv/bin/uvicorn agentego.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-8765}" \
  --log-level info
