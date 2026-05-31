"""
GET /health

Always responds, even when everything is broken.
STALE_FEED status set when last_event_ts > STALE_FEED_MINUTES ago.
"""

import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter

from app.models import HealthResponse, StoreHealthInfo

logger = logging.getLogger(__name__)
router = APIRouter()

_START_TIME = time.monotonic()


def _stale_feed_minutes() -> int:
    return int(os.getenv("STALE_FEED_MINUTES", "10"))


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    from app.db import get_db

    db_status = "ok"
    store_infos: dict[str, StoreHealthInfo] = {}

    try:
        db = get_db()
        db_ok = await db.ping()
        if not db_ok:
            db_status = "unavailable"
        else:
            store_infos = await _gather_store_health(db)
    except Exception as exc:
        logger.error("Health check DB query failed", extra={"error": str(exc)})
        db_status = "unavailable"

    overall_status = "ok" if db_status == "ok" else "degraded"
    uptime = time.monotonic() - _START_TIME

    return HealthResponse(
        status=overall_status,
        stores=store_infos,
        db_status=db_status,
        uptime_seconds=round(uptime, 1),
    )


async def _gather_store_health(db) -> dict[str, StoreHealthInfo]:
    stale_mins = _stale_feed_minutes()
    now = datetime.now(timezone.utc)
    stale_cutoff = (now - timedelta(minutes=stale_mins)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today_start = now.strftime("%Y-%m-%dT00:00:00Z")

    rows = await db.fetchall(
        """
        SELECT
            store_id,
            MAX(timestamp)  AS last_event_ts,
            COUNT(CASE WHEN timestamp >= ? THEN 1 END) AS event_count_today
        FROM events
        GROUP BY store_id
        """,
        (today_start,),
    )

    infos: dict[str, StoreHealthInfo] = {}
    for row in rows:
        last_ts: Optional[str] = row["last_event_ts"]
        is_stale = last_ts is not None and last_ts < stale_cutoff
        infos[row["store_id"]] = StoreHealthInfo(
            last_event_ts=last_ts,
            feed_status="STALE_FEED" if is_stale else "ok",
            event_count_today=int(row["event_count_today"] or 0),
        )
    return infos
