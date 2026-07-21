#!/usr/bin/env bash
# One bounded, fail-closed Bizarre Product Roulette acquisition + vision + rendering pass.
# Stdout: one script-composed delivery JSON object on success, exactly [SILENT] otherwise.
set -euo pipefail

# Reversible soft-audit flag: set BIZARRE_PRODUCT_LLAVA_AUDIT=0 to disable.
export BIZARRE_PRODUCT_LLAVA_AUDIT="${BIZARRE_PRODUCT_LLAVA_AUDIT:-1}"

collector_json=$(mktemp /tmp/bizarre-product-collector.XXXXXX.json)
commentary_json=$(mktemp /tmp/bizarre-product-commentary.XXXXXX.json)
trap 'rm -f "$collector_json" "$commentary_json"' EXIT

if ! python3 /home/cflux/.hermes/scripts/bizarre_product_collector.py --subreddit DiWHY >"$collector_json"; then
  printf '%s\n' '[SILENT]'
  exit 1
fi

if ! python3 /home/cflux/.hermes/scripts/bizarre_product_vlm_commentary.py \
  <"$collector_json" >"$commentary_json"; then
  printf '%s\n' '[SILENT]'
  exit 1
fi

python3 /home/cflux/.hermes/scripts/bizarre_product_roulette_render.py <"$commentary_json"
