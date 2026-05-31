"""
GET /stores/{store_id}/heatmap

Returns all zones from store_layout.json, even those with zero visits.
Score normalised 0–100 (max-visit zone = 100).
data_confidence = LOW when total customer sessions < 20.
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Request

from app.db import get_db
from app.models import HeatmapResponse, ZoneHeatmapEntry

logger = logging.getLogger(__name__)
router = APIRouter()

_LAYOUT_CACHE: dict[str, list[dict]] = {}


def _load_zone_ids(layout_path: str) -> list[str]:
    """Load zone IDs from store_layout.json, cached in memory."""
    if layout_path in _LAYOUT_CACHE:
        return [z["zone_id"] for z in _LAYOUT_CACHE[layout_path]]
    try:
        with open(layout_path, encoding="utf-8") as fh:
            layout = json.load(fh)
        zones = layout.get("zones", [])
        _LAYOUT_CACHE[layout_path] = zones
        return [z["zone_id"] for z in zones]
    except (FileNotFoundError, json.JSONDecodeError) as exc:
        logger.warning("Cannot load store_layout.json", extra={"path": layout_path, "error": str(exc)})
        return []


@router.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse)
async def get_heatmap(store_id: str, request: Request, window: str = "today") -> HeatmapResponse:
    db = get_db()
    layout_path = os.getenv("STORE_LAYOUT_PATH", "./data/store_layout.json")
    all_zone_ids = _load_zone_ids(layout_path)

    if window == "today":
        now = datetime.now(timezone.utc)
        ts_start = now.strftime("%Y-%m-%dT00:00:00Z")
        ts_end = now.strftime("%Y-%m-%dT23:59:59Z")
        window_clause = "AND timestamp BETWEEN ? AND ?"
        base_params: tuple = (store_id, ts_start, ts_end)
    else:
        window_clause = ""
        base_params = (store_id,)

    # Total unique customer sessions for data_confidence
    session_row = await db.fetchone(
        f"""
        SELECT COUNT(DISTINCT visitor_id) AS cnt
        FROM events
        WHERE store_id = ?
          AND is_staff = 0
          AND event_type = 'ENTRY'
          {window_clause}
        """,
        base_params,
    )
    total_sessions = session_row["cnt"] if session_row else 0
    data_confidence = "HIGH" if total_sessions >= 20 else "LOW"

    # Zone visit stats from ZONE_ENTER and ZONE_DWELL events
    visit_rows = await db.fetchall(
        f"""
        SELECT
            zone_id,
            COUNT(DISTINCT visitor_id)   AS visit_count,
            AVG(CASE WHEN event_type = 'ZONE_DWELL' THEN dwell_ms ELSE NULL END) AS avg_dwell_ms
        FROM events
        WHERE store_id = ?
          AND zone_id IS NOT NULL
          AND is_staff = 0
          AND event_type IN ('ZONE_ENTER', 'ZONE_DWELL')
          {window_clause}
        GROUP BY zone_id
        """,
        base_params,
    )

    zone_stats: dict[str, dict] = {}
    for row in visit_rows:
        zone_stats[row["zone_id"]] = {
            "visit_count": int(row["visit_count"]),
            "avg_dwell_ms": float(row["avg_dwell_ms"] or 0.0),
        }

    max_visits = max((s["visit_count"] for s in zone_stats.values()), default=0)

    def _normalise_score(visit_count: int) -> int:
        if max_visits == 0:
            return 0
        return round(visit_count / max_visits * 100)

    zones: list[ZoneHeatmapEntry] = []
    for zone_id in all_zone_ids:
        stats = zone_stats.get(zone_id, {"visit_count": 0, "avg_dwell_ms": 0.0})
        zones.append(ZoneHeatmapEntry(
            zone_id=zone_id,
            visit_count=stats["visit_count"],
            avg_dwell_ms=round(stats["avg_dwell_ms"], 1),
            score=_normalise_score(stats["visit_count"]),
            data_confidence=data_confidence,
        ))

    # If layout file missing, fall back to zones from DB only
    if not all_zone_ids:
        for zone_id, stats in zone_stats.items():
            zones.append(ZoneHeatmapEntry(
                zone_id=zone_id,
                visit_count=stats["visit_count"],
                avg_dwell_ms=round(stats["avg_dwell_ms"], 1),
                score=_normalise_score(stats["visit_count"]),
                data_confidence=data_confidence,
            ))

    return HeatmapResponse(store_id=store_id, zones=zones)
