"""
GET /stores/{store_id}/funnel

Unit of analysis is visitor session (unique visitor_id), not raw events.
REENTRY must not create a second session — same visitor_id counted once.
Drop-off percentages computed against the top of funnel (ENTRY stage).
"""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Request

from app.db import get_db
from app.models import FunnelResponse, FunnelStage

logger = logging.getLogger(__name__)
router = APIRouter()


def _drop_off_pct(current: int, previous: int) -> float:
    if previous == 0:
        return 0.0
    return round((previous - current) / previous * 100, 1)


@router.get("/stores/{store_id}/funnel", response_model=FunnelResponse)
async def get_funnel(store_id: str, request: Request, window: str = "today") -> FunnelResponse:
    db = get_db()

    if window == "today":
        now = datetime.now(timezone.utc)
        ts_start = now.strftime("%Y-%m-%dT00:00:00Z")
        ts_end = now.strftime("%Y-%m-%dT23:59:59Z")
        window_clause = "AND timestamp BETWEEN ? AND ?"
        base_params: tuple = (store_id, ts_start, ts_end)
    else:
        window_clause = ""
        base_params = (store_id,)

    # ENTRY: distinct visitor_ids that had at least one ENTRY event (REENTRY excluded to avoid double-count)
    entry_row = await db.fetchone(
        f"""
        SELECT COUNT(DISTINCT visitor_id) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type = 'ENTRY'
          AND is_staff = 0
          {window_clause}
        """,
        base_params,
    )
    entry_sessions = entry_row["cnt"] if entry_row else 0

    # ZONE_VISIT: distinct visitor_ids that had at least one ZONE_ENTER event
    zone_row = await db.fetchone(
        f"""
        SELECT COUNT(DISTINCT visitor_id) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type = 'ZONE_ENTER'
          AND is_staff = 0
          {window_clause}
        """,
        base_params,
    )
    zone_sessions = min(zone_row["cnt"] if zone_row else 0, entry_sessions)

    # BILLING_QUEUE: distinct visitor_ids with at least one BILLING_QUEUE_JOIN
    billing_row = await db.fetchone(
        f"""
        SELECT COUNT(DISTINCT visitor_id) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND is_staff = 0
          {window_clause}
        """,
        base_params,
    )
    billing_sessions = min(billing_row["cnt"] if billing_row else 0, entry_sessions)

    # PURCHASE: sessions that reached billing and did NOT abandon
    # (billing join with no subsequent abandon = converted)
    abandon_row = await db.fetchone(
        f"""
        SELECT COUNT(DISTINCT visitor_id) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type = 'BILLING_QUEUE_ABANDON'
          AND is_staff = 0
          {window_clause}
        """,
        base_params,
    )
    abandon_count = abandon_row["cnt"] if abandon_row else 0
    purchase_sessions = max(0, billing_sessions - abandon_count)

    funnel: list[FunnelStage] = [
        FunnelStage(stage="ENTRY",         sessions=entry_sessions,    drop_off_pct=0.0),
        FunnelStage(stage="ZONE_VISIT",    sessions=zone_sessions,     drop_off_pct=_drop_off_pct(zone_sessions, entry_sessions)),
        FunnelStage(stage="BILLING_QUEUE", sessions=billing_sessions,  drop_off_pct=_drop_off_pct(billing_sessions, zone_sessions)),
        FunnelStage(stage="PURCHASE",      sessions=purchase_sessions, drop_off_pct=_drop_off_pct(purchase_sessions, billing_sessions)),
    ]

    return FunnelResponse(store_id=store_id, funnel=funnel)
