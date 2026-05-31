#!/usr/bin/env bash
# run.sh — process all CCTV clips and seed events into the API
set -euo pipefail

CLIPS_DIR="${1:-./data/clips}"
OUTPUT_DIR="${2:-./data/events}"
LAYOUT="${LAYOUT:-./data/store_layout.json}"
POS="${POS:-./data/pos_transactions.csv}"
API_URL="${API_URL:-http://localhost:8000}"

mkdir -p "$OUTPUT_DIR"

echo "==> Processing clips from $CLIPS_DIR"
processed=0

for clip in "$CLIPS_DIR"/*.mp4; do
  [ -f "$clip" ] || { echo "No .mp4 files found in $CLIPS_DIR"; break; }
  echo "    $clip"
  store_id=$(python3 -m pipeline.detect \
    --clip "$clip" \
    --layout "$LAYOUT" \
    --pos "$POS" \
    --output "$OUTPUT_DIR")
  echo "    → ${OUTPUT_DIR}/${store_id}_events.jsonl"
  processed=$((processed + 1))
done

if [ "$processed" -eq 0 ]; then
  echo "WARNING: no clips processed — check $CLIPS_DIR"
  exit 0
fi

echo ""
echo "==> Seeding events into API at $API_URL"
python3 scripts/seed_events.py --events-dir "$OUTPUT_DIR" --api-url "$API_URL"

echo ""
echo "All done. Dashboard: http://localhost:3000"
