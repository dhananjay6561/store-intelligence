"""
GET /stores/{store_id}/metrics

Computes:
  - unique_visitors  (ENTRY events, is_staff=0)
  - conversion_rate  (sessions with BILLING_QUEUE_JOIN but no ABANDON ÷ unique visitors)
  - avg_dwell_by_zone (mean dwell_ms from ZONE_DWELL events)
  - queue_depth_current (latest queue_depth from BILLING_QUEUE_JOIN)
  - abandonment_rate  (BILLING_QUEUE_ABANDON ÷ BILLING_QUEUE_JOIN)

All figures computed live from DB on every request — no caching.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request

from app.db import get_db
from app.models import MetricsResponse

logger = logging.getLogger(__name__)
router = APIRouter()

_TODAY_WINDOW = "today"
_ALL_WINDOW = "all"


def _today_bounds() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    day_start = now.strftime("%Y-%m-%dT00:00:00Z")
    day_end = now.strftime("%Y-%m-%dT23:59:59Z")
    return day_start, day_end


@router.get("/stores/{store_id}/metrics", response_model=MetricsResponse)
async def get_metrics(store_id: str, request: Request, window: str = _TODAY_WINDOW) -> MetricsResponse:
    db = get_db()

    if window == _TODAY_WINDOW:
        ts_start, ts_end = _today_bounds()
        window_clause = "AND timestamp BETWEEN ? AND ?"
        window_params: tuple = (store_id, ts_start, ts_end)
        window_2params: tuple = (store_id, ts_start, ts_end)
    else:
        window_clause = ""
        window_params = (store_id,)
        window_2params = (store_id,)

    # Verify store exists
    store_check = await db.fetchone(
        f"SELECT COUNT(*) as cnt FROM events WHERE store_id = ? {window_clause}",
        window_params,
    )
    if store_check is None or store_check["cnt"] == 0:
        # Return zero-filled response for a store with no events — not a 404
        # (the pipeline may not have run yet for today)
        pass

    # unique_visitors: distinct visitor_id with at least one ENTRY event, excluding staff
    uv_row = await db.fetchone(
        f"""
        SELECT COUNT(DISTINCT visitor_id) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type IN ('ENTRY', 'REENTRY')
          AND is_staff = 0
          {window_clause.replace('AND timestamp', 'AND timestamp')}
        """,
        window_params,
    )
    unique_visitors: int = uv_row["cnt"] if uv_row else 0

    # conversion_rate: sessions with billing join but no abandon / unique visitors
    billing_join_row = await db.fetchone(
        f"""
        SELECT COUNT(DISTINCT visitor_id) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND is_staff = 0
          {window_clause.replace('AND timestamp', 'AND timestamp')}
        """,
        window_params,
    )
    billing_join_count: int = billing_join_row["cnt"] if billing_join_row else 0

    billing_abandon_row = await db.fetchone(
        f"""
        SELECT COUNT(DISTINCT visitor_id) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type = 'BILLING_QUEUE_ABANDON'
          AND is_staff = 0
          {window_clause.replace('AND timestamp', 'AND timestamp')}
        """,
        window_params,
    )
    billing_abandon_count: int = billing_abandon_row["cnt"] if billing_abandon_row else 0

    converted_count = max(0, billing_join_count - billing_abandon_count)
    conversion_rate: float = (converted_count / unique_visitors) if unique_visitors > 0 else 0.0

    # avg_dwell_by_zone: mean dwell_ms per zone from ZONE_DWELL events
    dwell_rows = await db.fetchall(
        f"""
        SELECT zone_id, AVG(dwell_ms) AS avg_dwell
        FROM events
        WHERE store_id = ?
          AND event_type = 'ZONE_DWELL'
          AND zone_id IS NOT NULL
          AND is_staff = 0
          {window_clause.replace('AND timestamp', 'AND timestamp')}
        GROUP BY zone_id
        """,
        window_params,
    )
    avg_dwell_by_zone: dict[str, float] = {
        row["zone_id"]: round(float(row["avg_dwell"]), 1)
        for row in dwell_rows
        if row["zone_id"]
    }

    # queue_depth_current: latest queue_depth value
    queue_row = await db.fetchone(
        f"""
        SELECT queue_depth
        FROM events
        WHERE store_id = ?
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND queue_depth IS NOT NULL
          {window_clause.replace('AND timestamp', 'AND timestamp')}
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        window_params,
    )
    queue_depth_current: int = int(queue_row["queue_depth"]) if queue_row and queue_row["queue_depth"] is not None else 0

    # abandonment_rate: abandon count / join count
    bj_total_row = await db.fetchone(
        f"""
        SELECT COUNT(*) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND is_staff = 0
          {window_clause.replace('AND timestamp', 'AND timestamp')}
        """,
        window_params,
    )
    ba_total_row = await db.fetchone(
        f"""
        SELECT COUNT(*) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type = 'BILLING_QUEUE_ABANDON'
          AND is_staff = 0
          {window_clause.replace('AND timestamp', 'AND timestamp')}
        """,
        window_params,
    )
    bj_total = bj_total_row["cnt"] if bj_total_row else 0
    ba_total = ba_total_row["cnt"] if ba_total_row else 0
    abandonment_rate: float = (ba_total / bj_total) if bj_total > 0 else 0.0

    return MetricsResponse(
        store_id=store_id,
        window=window,
        unique_visitors=unique_visitors,
        conversion_rate=round(conversion_rate, 4),
        avg_dwell_by_zone=avg_dwell_by_zone,
        queue_depth_current=queue_depth_current,
        abandonment_rate=round(abandonment_rate, 4),
        generated_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    )
