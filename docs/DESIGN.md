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

### SSE attempted, then removed

SSE was the first implementation: a `/events/stream` FastAPI endpoint with a `while True: await asyncio.sleep(2)` generator yielding metric payloads. It worked in isolation but caused the uvicorn process to spin at 97% CPU and become unresponsive to all other requests after 3–4 minutes. The generator's async loop consumed the event loop even between connected clients, blocking health checks and ingest calls.

The fix was not to optimise the generator — it was to remove it entirely. The dashboard now uses `setInterval` polling four independent REST endpoints at 4-second and 10-second intervals. The server goes idle between polls. This is less "real-time" in name but functionally equivalent for a retail analytics use case where metrics don't change faster than once every few seconds.

The SSE endpoint is documented here as a deliberate trade-off, not an oversight.

### What Is Displayed
- **Conversion rate sparkline** — 30-point rolling history via Chart.js, polling every 4s
- **Zone heatmap** — 0–100 normalised scores by visit count, polling every 10s
- **Queue depth indicator** — progress bar turns amber at 5, red at 8
- **Anomaly panel** — badge count + list, polling every 10s

---

## AI-Assisted Decisions

Each section below follows the format: what the problem was, what the LLM suggested when asked, what was actually built, and crucially where the suggestion was wrong or incomplete.

---

### Decision 1: Re-ID without an embedding model

**The problem:** ByteTrack loses a track_id when a person exits the frame and re-enters later — even seconds later after a brief occlusion. Without Re-ID, every re-entry creates a new ENTRY event and inflates unique_visitors. Asked the LLM how to handle same-person re-entry in a batch offline pipeline.

**What the LLM suggested:** OSNet or FastReID — generate 512-dimensional appearance embeddings per track, cosine-similarity match against a gallery of recently-lost tracks. Cited papers showing 90%+ rank-1 accuracy on Market-1501 benchmark.

**What was built instead:** Centroid proximity (< 200px) + torso colour histogram (18×16 bin HSV, Bhattacharyya distance < 0.3). 5-minute window.

**Where the suggestion was wrong:** The LLM assumed a GPU was available and that the Market-1501 benchmark was relevant. Market-1501 tests across disjoint camera networks with identical lighting and clean crops — the Brigade store has partial occlusions, aisle reflections, and clothing that looks similar in histogram space anyway (customers in a beauty store skew toward similar casual clothing). An OSNet model pretrained on pedestrian re-ID data does not transfer well to retail without fine-tuning data we don't have. The simpler geometric approach is honest about its limitations: it works for same-camera re-entry (which is the common case) and fails for cross-camera floor transitions (which is the edge case documented in README Known Limitations).

**Worst-case failure mode for the implemented approach:** Two customers wearing similar light-coloured tops enter through the same entry point within 5 minutes of each other. The histogram distance will be low and the centroids will overlap near the entry threshold — this produces a false Re-ID merge where customer B's session inherits customer A's visitor_id. In practice, the entry threshold area is a bottleneck, so this scenario produces a wrong REENTRY rather than a wrong session count in the funnel, because the first visitor's session is already closed by EXIT before the second arrives.

---

### Decision 2: Anomaly thresholds — fixed vs. adaptive

**The problem:** What should trigger a BILLING_QUEUE_SPIKE and a CONVERSION_DROP? Both require a threshold above which something is considered anomalous.

**What the LLM suggested:** Statistical control chart approach — compute 30-day rolling mean and standard deviation, emit WARN at mean + 1σ and CRITICAL at mean + 2σ. For conversion drop, use same-hour 7-day comparison to control for hourly patterns (foot traffic at 2pm is different from 9am).

**What was built instead:** Fixed environment-variable thresholds (default WARN=5, CRITICAL=8 for queue; 70% of 7-day mean for conversion). Simple rolling average, no hour bucketing.

**Why the suggestion was rejected — and one part kept:** The 2σ approach requires at minimum 20–30 data points for σ to be meaningful. A store going live on day 1 has zero history; the model would emit no anomalies for weeks until enough data accumulates, which is the opposite of what a new deployment needs. Fixed thresholds with operator overrides work on day 0.

The part that was kept: the 7-day comparison window for conversion drop is sound, and the day-0 edge case is handled explicitly (`if avg_rate == 0.0: return None`). The hour-bucketing was dropped because the Brigade Bangalore data spans a single day — computing same-hour averages from one day's data is meaningless and would produce constant CONVERSION_DROP false positives in the morning hours.

---

### Decision 3: Nested metadata vs. flat schema

**The problem:** The event schema has fields that are only relevant for specific event types — `queue_depth` only for `BILLING_QUEUE_JOIN`, `sku_zone` only for zone events. How should optional type-specific fields be structured?

**What the LLM suggested:** Flat top-level schema. All fields present on every event, set to `null` when not applicable. Argument: simpler deserialization, no nested traversal, consistent structure.

**What was built:** Nested `metadata` object for `queue_depth`, `sku_zone`, `session_seq`, `low_conf`.

**Where the suggestion was partially right:** For the API response layer and SQL queries, the flat suggestion would have been fine. Most queries only touch the top-level indexed fields (`store_id`, `event_type`, `timestamp`, `visitor_id`, `is_staff`). The metadata fields are never queried directly in WHERE clauses.

**Why nested was chosen anyway:** The `raw_json` column stores the complete event for audit. With a flat schema, `raw_json` and the individual columns are redundant. The nested structure makes `raw_json` the authoritative record and the individual columns a materialised index into it. More importantly, `session_seq` was added mid-build after discovering that ZONE_EXIT and ZONE_ENTER events from the same frame had identical timestamps — ordering them required an ordinal field. A flat schema would have made this a top-level field, which felt wrong for what is essentially processing metadata. It belongs in `metadata`.

---

## Known Investigation: HAIRCARE and FRAGRANCE at Zero Visits

The heatmap returns 0 visits for HAIRCARE and FRAGRANCE across all five clips. There are two possible explanations, and they require different fixes:

**Hypothesis A — FOV mismatch (most likely):** The zone polygons for HAIRCARE and FRAGRANCE were drawn against an assumed camera layout where CAM_3 covers the back half of the store floor. If CAM_3 is actually pointed at a different section, or if those product areas are simply outside all five camera fields of view, centroids will never fall inside those polygons.

Diagnostic: Take a single frame from CAM_3, overlay the zone polygon coordinates on it, and check whether the HAIRCARE/FRAGRANCE polygons land on the actual wall those products are displayed on. If the polygons land in an aisle or on a wall with different products, recalibrate the coordinates against a measured floor plan.

**Hypothesis B — Zone polygon calibration error:** The polygons are defined in pixel coordinates against a 1920×1080 frame. If the actual camera resolution, crop, or lens distortion shifts the coordinate space, a polygon that looks correct on paper misses the real floor area.

Diagnostic: Log centroid coordinates for all CAM_3 detections into a scatter plot and overlay the zone polygons. Any centroids consistently clustering outside the polygon boundaries indicate a calibration offset.

**Why it was not fixed before submission:** Without a floor plan mapped to actual pixel coordinates for each camera, recalibrating is guesswork. The zero-visit result is reported honestly in the heatmap with `data_confidence: HIGH` (because there are >20 sessions, just none in those zones). Surfacing a zero rather than hiding it is the correct behaviour — it flags a data quality issue rather than masking it.
