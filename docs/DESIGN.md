# System Design

## Architecture Overview

The system is a four-stage pipeline that converts raw CCTV footage into live retail analytics.

```
Raw CCTV Clips (5 cameras, ~20min each, 1080p)
        │
        ▼
┌───────────────────────────────┐
│     Detection Layer           │  pipeline/
│  YOLOv8n → ByteTrack → Re-ID  │  detect.py, tracker.py, emit.py
│  Staff classifier (histogram) │  staff.py, zone.py
│  Zone hit-test (polygons)     │  pos_correlator.py
└──────────────┬────────────────┘
               │ JSONL events (one file per store)
               ▼
┌───────────────────────────────┐
│     Event Ingest              │  app/ingestion.py
│  Pydantic validation → SQLite │
│  Dedup by event_id (IGNORE)   │
└──────────────┬────────────────┘
               │
               ▼
┌───────────────────────────────┐
│     Intelligence API          │  app/  (FastAPI + aiosqlite)
│  /metrics  /funnel  /heatmap  │
│  /anomalies  /health          │
│  /events/ingest  /events/stream│
└──────────────┬────────────────┘
               │ SSE (Server-Sent Events, 2s interval)
               ▼
┌───────────────────────────────┐
│     Live Dashboard            │  dashboard/
│  Conversion rate sparkline    │
│  Zone heatmap (0-100 scores)  │
│  Queue depth indicator        │
│  Anomaly panel                │
└───────────────────────────────┘
```

## Stage 1: Detection Layer

### Object Detection
YOLOv8n processes each video frame at native FPS (15fps). Person detections (`class=0`) are the sole input to the tracker. Confidence scores are passed through verbatim — events with `confidence < 0.4` are flagged `low_conf=true` in metadata but are never dropped. Suppressing low-confidence detections would create systematic blind spots in crowded or partially occluded scenes.

### Tracking and Visitor Identity
ByteTrack (built into ultralytics v8) maintains persistent `track_id`s across frames using a Kalman filter for motion prediction and IoU matching for re-association. Each `track_id` maps to a stable `visitor_id` token (`VIS_<6hex>`), generated once per session in `tracker.py`.

### Re-ID
When a track is lost (occlusion or exit), the last known centroid and colour histogram are cached for 5 minutes. When a new track appears within 200px of a lost track's last position, histograms are compared (Bhattacharyya distance < 0.3). A match reuses the same `visitor_id` and emits a `REENTRY` event rather than a fresh `ENTRY`. This prevents double-counting returning visitors in the conversion funnel.

### Staff Detection
Two independent heuristics are applied per track in `staff.py`:
1. **Torso colour histogram** — a 18×16 bin HSV histogram of the torso ROI (middle third of bbox height) is compared against a reference histogram derived from the uniform HSV range in `store_layout.json`. Bhattacharyya distance < 0.35 = uniform match.
2. **Zone frequency** — staff traverse more than 4 distinct zones per session; customers rarely do. This catches staff who are out of uniform or captured from angles that defeat histogram matching.

Staff events are stored with `is_staff=1`. Every API query filters them out with `WHERE is_staff = 0`.

### Zone Classification
Each centroid is tested against zone polygons from `store_layout.json` using the ray-casting algorithm. The data for this project uses 10 zones derived from the real store's product category distribution: MAKEUP (54% of POS items), SKINCARE (27%), BATH_BODY, HAIRCARE, FRAGRANCE, PERSONAL_CARE, BILLING, QUEUE_AREA, ENTRY_LOBBY, NEW_ARRIVALS.

## Stage 2: Event Stream

### Schema Rationale
The event schema was designed to be flat at the top level with a nested `metadata` object for optional fields (`queue_depth`, `sku_zone`, `session_seq`). This keeps the hot SQL path (indexed columns: `store_id`, `timestamp`, `visitor_id`, `event_type`) direct and avoids JSON parsing in queries. The `raw_json` column stores the full original event for audit purposes.

`session_seq` is an ordinal counter within a visitor's session, allowing downstream ordering of events without relying on timestamp precision.

### JSONL Emission
`emit.py` validates every event against the Pydantic schema before appending to the JSONL file. The emitter tracks a rejected count separately so pipeline operators can inspect validation failures without losing accepted events.

## Stage 3: Intelligence API

### Framework and Database
FastAPI with aiosqlite was chosen for this challenge. SQLite with WAL mode supports concurrent reads from multiple connections while a single writer appends events. For the volume of 5 stores × 5 cameras × 20-minute clips (~150k events total), SQLite is more than sufficient. For production scale (millions of daily events, multiple concurrent API replicas), the `DATABASE_URL` environment variable accepts a PostgreSQL asyncpg connection string — the SQL is compatible with minimal changes.

### Query Strategy
All endpoints compute live from the DB on every request — no in-process cache. This is a deliberate trade-off: the event volume is small enough that queries complete in < 10ms, and caching would complicate the correctness guarantee ("every metric varies with ingested data").

The `sessions` view pre-aggregates per-visitor state. Funnel and conversion endpoints use `COUNT(DISTINCT visitor_id)` rather than counting raw events to ensure REENTRY events don't inflate session counts.

### Idempotency
`INSERT OR IGNORE INTO events ... WHERE event_id = ?` makes the ingest endpoint safe to call multiple times with the same payload. The response distinguishes between newly accepted events and duplicates, which matters for pipeline retry logic.

## Stage 4: Live Dashboard

### SSE vs WebSocket
Server-Sent Events were chosen over WebSockets for two reasons: the dashboard only needs server→client data flow (no bidirectional communication), and SSE works through HTTP/1.1 proxies without upgrade handshakes. The `/events/stream` endpoint yields a JSON-serialised `MetricsResponse` every 2 seconds. The client reconnects automatically on connection drop.

### What Is Displayed
- **Conversion rate sparkline** — 30-point rolling history via Chart.js, updated every SSE tick
- **Zone heatmap** — colour-coded 0–100 normalised scores, polled every 10 seconds
- **Queue depth indicator** — card background changes from neutral → amber → red at thresholds 5 and 8
- **Anomaly panel** — badge count + list of active anomalies, polled every 10 seconds

---

## AI-Assisted Decisions

### Decision 1: Re-ID Strategy

**What was asked:** How to handle the same physical person exiting one camera's frame and re-entering a few minutes later — avoiding double-counting in the conversion funnel.

**What the model suggested:** Using a dedicated Re-ID model such as OSNet or FastReID to generate 512-dimensional feature embeddings per track, then performing cosine-similarity matching against a gallery of recently lost tracks.

**What was implemented:** Bounding-box centroid proximity (< 200px) + torso colour histogram comparison (Bhattacharyya distance < 0.3). No dedicated Re-ID model.

**Why it diverged:** The challenge explicitly prohibits GPU requirements ("No GPU-heavy OSNet"). More importantly, for batch processing of 20-minute beauty store clips at 15fps on a single machine, the histogram approach runs in microseconds per frame vs. ~2ms per frame for an embedding model. The accuracy trade-off is acceptable: in a constrained retail space, two people re-entering within 200px of the same spot within 5 minutes is unlikely to be a coincidence.

---

### Decision 2: Anomaly Thresholds

**What was asked:** What thresholds should trigger billing queue spike anomalies, and how should conversion drop be computed.

**What the model suggested:** Dynamic thresholds derived from 30-day rolling mean + 2σ standard deviation — WARN when queue depth exceeds mean + 1σ, CRITICAL when it exceeds mean + 2σ. For conversion drop, compare today's hourly rate against the same hour's 7-day average.

**What was implemented:** Fixed thresholds configurable via environment variables (`QUEUE_SPIKE_WARN_THRESHOLD=5`, `QUEUE_SPIKE_CRITICAL_THRESHOLD=8`). Conversion drop uses a simple 7-day rolling average (not hour-bucketed).

**Why it diverged:** Dynamic σ-based thresholds require sufficient historical data to be meaningful — a store that has been live for one week has no reliable σ estimate. Fixed thresholds with environment variable overrides give operators immediate control and work from day 0. The hour-bucketed comparison was dropped because the Brigade Bangalore POS data only spans one day, making same-hour averaging impractical.

---

### Decision 3: Session Seq and Metadata Field Design

**What was asked:** Should optional, event-type-specific fields (queue_depth, sku_zone) live at the top level of the event schema or inside a nested metadata object?

**What the model suggested:** Flat schema with all fields at the top level, using `None` for inapplicable fields. Rationale: simpler deserialization, no nested traversal.

**What was implemented:** Nested `metadata` object containing `queue_depth`, `sku_zone`, `session_seq`, and `low_conf`.

**Why it diverged:** The hot SQL path only ever queries `store_id`, `timestamp`, `visitor_id`, `event_type`, `zone_id`, and `is_staff` — all at the top level and all indexed. The optional fields are only materialised when computing specific metrics. Keeping them in `metadata` prevents column proliferation and makes the `raw_json` audit trail self-contained. The `session_seq` field was added after observing that multiple events for the same visitor at the same millisecond timestamp (common during fast zone transitions) could not be ordered without an explicit ordinal.
