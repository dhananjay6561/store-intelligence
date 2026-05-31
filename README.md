# Store Intelligence — Brigade Bangalore

Retail store analytics system that processes CCTV footage from **Purplle Brigade Road** and produces a live intelligence API with conversion funnel, zone heatmap, and anomaly detection.

---

## 5-Command Setup

```bash
git clone <repo-url> store-intelligence && cd store-intelligence
bash bootstrap.sh                         # install deps + copy .env
cp -r /path/to/cctv-clips data/clips/     # place .mp4 files here
docker compose up --build                 # start API + dashboard
bash pipeline/run.sh                      # process clips → seed API
```

Dashboard: **http://localhost:3000**  
API Docs: **http://localhost:8000/docs**

---

## Detection Pipeline

The pipeline processes CCTV footage using YOLOv8n + ByteTrack and emits structured JSONL events.

### Run on all clips
```bash
# With default paths (clips in ./data/clips, events to ./data/events)
bash pipeline/run.sh

# Custom paths
CLIPS_DIR=/Volumes/CCTV/footage  OUTPUT_DIR=./data/events  bash pipeline/run.sh
```

Output: one JSONL file per store at `./data/events/{store_id}_events.jsonl`

### What gets emitted

| Event Type | Trigger |
|---|---|
| `ENTRY` | Person crosses entry threshold inbound |
| `EXIT` | Person crosses entry threshold outbound or track lost |
| `REENTRY` | Known visitor_id detected again within 5 min |
| `ZONE_ENTER` | Centroid enters a zone polygon |
| `ZONE_EXIT` | Centroid leaves a zone polygon |
| `ZONE_DWELL` | 30 continuous seconds in a zone |
| `BILLING_QUEUE_JOIN` | Person enters billing zone when queue_depth > 0 |
| `BILLING_QUEUE_ABANDON` | Person leaves billing with no POS match in 10 min |

---

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/events/ingest` | Batch ingest (up to 500 events, idempotent by event_id) |
| `GET` | `/stores/{id}/metrics` | Conversion rate, dwell, queue depth, abandonment |
| `GET` | `/stores/{id}/funnel` | 4-stage session funnel with drop-off % |
| `GET` | `/stores/{id}/heatmap` | Zone visit scores (0–100 normalised) |
| `GET` | `/stores/{id}/anomalies` | Active anomalies with severity + actions |
| `GET` | `/health` | Feed status, last event ts, DB status |
| `GET` | `/events/stream?store_id=` | SSE stream for live dashboard |

Store ID for Brigade Bangalore: `ST1008`

### Example calls
```bash
# Ingest events from pipeline output
curl -X POST http://localhost:8000/events/ingest \
  -H "Content-Type: application/json" \
  -d @data/events/ST1008_events.jsonl

# Get metrics
curl "http://localhost:8000/stores/ST1008/metrics"

# Get full funnel
curl "http://localhost:8000/stores/ST1008/funnel"
```

---

## Live Dashboard

Dashboard runs at **http://localhost:3000**

To simulate real-time updates: run `bash pipeline/run.sh` while the API is up — events feed into the DB as each clip is processed and the dashboard updates every 2 seconds via SSE.

| Widget | Description |
|---|---|
| Conversion Rate | Live sparkline (last 30 ticks, updates every 2s) |
| Zone Heatmap | Colour-coded score grid (0–100, polled every 10s) |
| Queue Depth | Green → amber → red at thresholds 5 and 8 |
| Anomaly Panel | Active anomaly count + descriptions + actions |

---

## Running Tests

```bash
source .venv/bin/activate
pytest --cov=app --cov=pipeline --cov-report=term-missing
```

Coverage target: **> 70% statement coverage**

---

## Store Data

| File | Contents |
|---|---|
| `data/store_layout.json` | Zone polygons for 10 zones (MAKEUP, SKINCARE, BILLING, etc.) |
| `data/pos_transactions.csv` | 24 real POS orders from Brigade Bangalore, 10-Apr-2026 |
| `data/clips/` | Place CCTV .mp4 files here before running pipeline |

---

## Architecture

See [docs/DESIGN.md](docs/DESIGN.md) for the full architecture, stage-by-stage breakdown, and AI-Assisted Decisions.

See [docs/CHOICES.md](docs/CHOICES.md) for the three key engineering trade-offs: model selection, schema design, and database choice.
