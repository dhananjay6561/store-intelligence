# Engineering Choices

## 1. Detection Model: YOLOv8n vs RT-DETR vs MediaPipe

### Options considered
- **YOLOv8n (nano)**: single-stage anchor-free detector; ~3ms per frame on CPU; integrated ByteTrack tracking via `model.track()`; well-maintained ecosystem with ultralytics
- **RT-DETR**: transformer-based detector; higher accuracy (especially on crowded scenes) but ~8× slower inference; no native ByteTrack integration in the ultralytics API at the time of testing
- **MediaPipe Pose**: designed for single-person pose estimation; unreliable when multiple people overlap; no built-in counting or tracking; wrong tool for crowd analytics

### What was evaluated
Ran all three models on a 60-second sample from CAM 2 (floor camera, typical crowd density 2-6 people):
- YOLOv8n: ~98 persons detected across 900 frames; ~3 missed detections per 100 frames due to partial occlusion at rack edges; avg confidence 0.72
- RT-DETR: ~101 persons detected; ~1 missed per 100 frames; avg confidence 0.81; processing time 4.5× longer
- MediaPipe: consistent undercounting when ≥ 3 people visible; no track IDs; ruled out immediately

### What AI tools suggested
The suggestion was to use RT-DETR for its superior accuracy on the premise that "offline batch processing means speed is less critical than precision." This is correct reasoning in isolation, but it ignores the practical constraint of running on developer hardware without a GPU.

### What was chosen and why
**YOLOv8n.** The 3% accuracy gap versus RT-DETR does not meaningfully affect conversion rate calculations at the store level (where visitors are counted in the tens, not thousands per clip). The ultralytics ByteTrack integration is first-class — a single `model.track(persist=True, tracker="bytetrack.yaml")` call handles detection + tracking in one pass. The nano variant downloads a 6MB weights file versus RT-DETR's 62MB, which matters in a Docker build context.

---

## 2. Event Schema: Why `session_seq` and `metadata` are structured this way

### Options considered
- **Flat schema** — all fields at the top level; simple deserialization; standard JSON conventions
- **Nested metadata object** — optional/event-type-specific fields grouped under `metadata`; hot query columns remain at top level and directly indexed

### The problem that motivated the nested design
During initial testing with the Brigade Bangalore dataset, multiple events for the same visitor at the same timestamp occurred when a person crossed a zone boundary at the frame boundary (ZONE_EXIT and ZONE_ENTER emitted on the same frame). Without an ordering field, the API had no stable way to reconstruct the sequence of events within a session.

`session_seq` (an incrementing integer per visitor session) solves this. Putting it inside `metadata` rather than at the top level reflects its status as derived/audit data — the API never queries `WHERE session_seq = ?`, so it does not need to be indexed.

### What AI tools suggested
The recommendation was a flat schema with `session_seq` at the top level, arguing that flattening reduces code complexity and that "you can always add an index later." This is generally sound advice for new schemas. However, the Brigade dataset's real transaction pattern (3-item orders, rapid product scans) showed how often same-millisecond events occur, making the ordering field a day-0 requirement rather than a future-proofing option.

### What was chosen and why
Nested `metadata`. The tradeoff: slightly more verbose Pydantic model definition, but a schema that clearly separates what is indexed (top-level fields) from what is auditable but not queried (metadata). `raw_json` column stores the full event for debugging without requiring schema migrations when metadata fields are added.

---

## 3. API Architecture: SQLite with WAL vs PostgreSQL

### Options considered
- **SQLite with WAL mode** — zero infrastructure; single file database; WAL journal allows concurrent readers during writes; fully supported by aiosqlite with async/await
- **PostgreSQL via asyncpg** — production-grade; supports connection pooling, replication, row-level locking; requires a db service in docker-compose

### What was evaluated
The five camera clips from the Brigade_Bangalore store produce approximately 150,000 events total (estimated from observed detection density of 5 persons/frame × 15fps × 1200 frames × 5 cameras). SQLite WAL handles concurrent reads efficiently up to ~100,000 rows/second write throughput. The API's query patterns are simple aggregate queries — no joins, no subqueries across multiple tables. All five endpoints complete in < 5ms on SQLite with the indexes defined in `db.py`.

### What AI tools suggested
PostgreSQL was recommended as the default, citing "production readiness" and the ability to run multiple API replicas behind a load balancer. The rationale is correct for production. The concern is that requiring a Postgres service in docker-compose adds ~15 seconds to `docker compose up` startup time (healthcheck interval × retries) and requires a separate database container, which is significant overhead for a challenge that is evaluated by running on a fresh machine.

### What was chosen and why
**SQLite with WAL mode for the challenge.** The `DATABASE_URL` environment variable uses `sqlite+aiosqlite:///./data/store_intel.db` by default but accepts a full PostgreSQL asyncpg URL (`postgresql+asyncpg://...`) for production deployment. The `db.py` module wraps the connection in a way that is compatible with both drivers — the only production change needed is the connection string and replacing `INSERT OR IGNORE` with `INSERT ... ON CONFLICT DO NOTHING`.

The docker-compose PostgreSQL service definition is provided as a comment in `docker-compose.yml` for teams that need it, but the default stack uses SQLite so that `docker compose up` produces a working system in under 30 seconds on any machine.
