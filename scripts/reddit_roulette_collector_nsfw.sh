#!/usr/bin/env bash
set -euo pipefail
exec python3 /home/cflux/.hermes/scripts/reddit_roulette_collector.py --nsfw "$@"
