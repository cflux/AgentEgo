#!/usr/bin/env bash
# Fixed Lab Rat reproduction path: only r/DiWHY, no shell interpolation.
set -euo pipefail
exec python3 /home/cflux/.hermes/scripts/bizarre_product_collector.py --subreddit DiWHY "$@"
