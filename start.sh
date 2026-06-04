#!/usr/bin/env bash
# Startup for Hugging Face Spaces: launch the API, then seed the bundled events.
# Re-seeding on every boot is safe — ingestion dedupes by event_id.
set -e

PORT="${PORT:-7860}"
DB_FILE="./data/store_intel.db"

# Drop any stale DB / WAL files so the schema rebuilds cleanly on a fresh container.
rm -f "$DB_FILE" "$DB_FILE-shm" "$DB_FILE-wal"

python3 -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" &
SERVER_PID=$!

echo "Waiting for API on :$PORT ..."
for _ in $(seq 1 60); do
  if python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:${PORT}/health')" 2>/dev/null; then
    echo "API healthy — seeding events"
    break
  fi
  sleep 1
done

python3 scripts/seed_events.py --events-dir data/events --api-url "http://localhost:${PORT}" \
  || echo "WARN: seed step failed, dashboard may be empty"

wait "$SERVER_PID"
