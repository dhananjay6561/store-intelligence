"""
POST /events/ingest — validate, deduplicate, and persist a batch of store events.

Each event is validated independently so a single malformed record does not
block the rest. Deduplication is by event_id (INSERT OR IGNORE).
"""

import json
import logging
from typing import Any

from fastapi import APIRouter, Request
from pydantic import ValidationError

from app.db import get_db
from app.models import IngestError, IngestRequest, IngestResponse, StoreEvent

logger = logging.getLogger(__name__)
router = APIRouter()

_INSERT_SQL = """
INSERT OR IGNORE INTO events (
    event_id, store_id, camera_id, visitor_id, event_type,
    timestamp, zone_id, dwell_ms, is_staff, confidence,
    queue_depth, sku_zone, session_seq, raw_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _event_to_row(event: StoreEvent) -> tuple:
    return (
        event.event_id,
        event.store_id,
        event.camera_id,
        event.visitor_id,
        event.event_type.value,
        event.timestamp,
        event.zone_id,
        event.dwell_ms,
        1 if event.is_staff else 0,
        event.confidence,
        event.metadata.queue_depth,
        event.metadata.sku_zone,
        event.metadata.session_seq,
        event.model_dump_json(),
    )


@router.post("/events/ingest", response_model=IngestResponse)
async def ingest_events(request: Request, payload: IngestRequest) -> IngestResponse:
    db = get_db()
    trace_id: str = getattr(request.state, "trace_id", "unknown")

    raw_events = payload.events
    accepted_rows: list[tuple] = []
    errors: list[IngestError] = []
    duplicate_ids: set[str] = set()

    # Check for duplicates already in DB
    if raw_events:
        existing_ids = await _fetch_existing_ids(db, [e.event_id for e in raw_events])
        duplicate_ids = existing_ids

    for idx, event in enumerate(raw_events):
        if event.event_id in duplicate_ids:
            continue
        accepted_rows.append(_event_to_row(event))

    # Bulk insert — INSERT OR IGNORE handles any remaining duplicates
    if accepted_rows:
        await db.executemany(_INSERT_SQL, accepted_rows)

    # Re-count actual new rows vs duplicates
    new_ids = {row[0] for row in accepted_rows}
    post_existing = await _fetch_existing_ids(db, list(new_ids))
    actually_inserted = len(post_existing - duplicate_ids)
    late_duplicates = len(new_ids) - actually_inserted

    total_duplicates = len(duplicate_ids) + late_duplicates
    accepted_count = len(accepted_rows) - late_duplicates

    logger.info(
        "Ingest complete",
        extra={
            "trace_id": trace_id,
            "total": len(raw_events),
            "accepted": accepted_count,
            "rejected": len(errors),
            "duplicates": total_duplicates,
        },
    )

    return IngestResponse(
        accepted=accepted_count,
        rejected=len(errors),
        duplicates=total_duplicates,
        errors=errors,
    )


async def _fetch_existing_ids(db: Any, event_ids: list[str]) -> set[str]:
    if not event_ids:
        return set()
    placeholders = ",".join("?" * len(event_ids))
    rows = await db.fetchall(
        f"SELECT event_id FROM events WHERE event_id IN ({placeholders})",
        tuple(event_ids),
    )
    return {row["event_id"] for row in rows}


@router.post("/events/ingest/raw")
async def ingest_raw_jsonl(request: Request) -> IngestResponse:
    """Accept raw JSONL body — one JSON object per line — for bulk pipeline ingestion."""
    trace_id: str = getattr(request.state, "trace_id", "unknown")
    body = await request.body()
    lines = [l.strip() for l in body.decode("utf-8").split("\n") if l.strip()]

    parsed: list[StoreEvent] = []
    errors: list[IngestError] = []

    for idx, line in enumerate(lines):
        try:
            data = json.loads(line)
            parsed.append(StoreEvent.model_validate(data))
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            event_id = None
            try:
                event_id = json.loads(line).get("event_id")
            except Exception:
                pass
            errors.append(IngestError(index=idx, event_id=event_id, reason=str(exc)))

    if not parsed:
        return IngestResponse(accepted=0, rejected=len(errors), duplicates=0, errors=errors)

    from app.models import IngestRequest as _IR
    sub_request = type("_FakeRequest", (), {"state": request.state})()
    sub_payload = _IR(events=parsed)
    result = await ingest_events(sub_request, sub_payload)
    return IngestResponse(
        accepted=result.accepted,
        rejected=result.rejected + len(errors),
        duplicates=result.duplicates,
        errors=result.errors + errors,
    )
