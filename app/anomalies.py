"""
GET /stores/{store_id}/anomalies

Four anomaly types detected from live event data:
  BILLING_QUEUE_SPIKE — queue_depth > threshold for > 2 minutes
  CONVERSION_DROP     — today's rate < 70% of 7-day rolling average
  DEAD_ZONE           — no ZONE_ENTER events in 30 minutes during open hours
  STALE_FEED          — no events from any camera in > 10 minutes
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Request

from app.db import get_db
from app.models import Anomaly, AnomaliesResponse, AnomalySeverity

logger = logging.getLogger(__name__)
router = APIRouter()

# Thresholds read from environment so they vary with deployment config
def _queue_warn_threshold() -> int:
    return int(os.getenv("QUEUE_SPIKE_WARN_THRESHOLD", "5"))

def _queue_critical_threshold() -> int:
    return int(os.getenv("QUEUE_SPIKE_CRITICAL_THRESHOLD", "8"))

def _stale_feed_minutes() -> int:
    return int(os.getenv("STALE_FEED_MINUTES", "10"))

def _dead_zone_minutes() -> int:
    return int(os.getenv("DEAD_ZONE_MINUTES", "30"))

def _conversion_drop_pct() -> float:
    return float(os.getenv("CONVERSION_DROP_PCT", "0.70"))


@router.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse)
async def get_anomalies(store_id: str, request: Request) -> AnomaliesResponse:
    db = get_db()
    now = datetime.now(timezone.utc)
    anomalies: list[Anomaly] = []

    # --- BILLING_QUEUE_SPIKE ---
    queue_spike = await _detect_queue_spike(db, store_id, now)
    if queue_spike:
        anomalies.append(queue_spike)

    # --- CONVERSION_DROP ---
    conversion_drop = await _detect_conversion_drop(db, store_id, now)
    if conversion_drop:
        anomalies.append(conversion_drop)

    # --- DEAD_ZONE ---
    dead_zone = await _detect_dead_zone(db, store_id, now)
    if dead_zone:
        anomalies.append(dead_zone)

    # --- STALE_FEED ---
    stale_feed = await _detect_stale_feed(db, store_id, now)
    if stale_feed:
        anomalies.append(stale_feed)

    return AnomaliesResponse(store_id=store_id, anomalies=anomalies)


async def _detect_queue_spike(db, store_id: str, now: datetime) -> Optional[Anomaly]:
    warn_threshold = _queue_warn_threshold()
    critical_threshold = _queue_critical_threshold()
    spike_window_minutes = 2

    window_start = (now - timedelta(minutes=spike_window_minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    row = await db.fetchone(
        """
        SELECT MAX(queue_depth) AS max_q, timestamp
        FROM events
        WHERE store_id = ?
          AND event_type = 'BILLING_QUEUE_JOIN'
          AND queue_depth IS NOT NULL
          AND timestamp >= ?
        ORDER BY timestamp DESC
        LIMIT 1
        """,
        (store_id, window_start),
    )
    if not row or row["max_q"] is None:
        return None

    max_q = int(row["max_q"])
    detected_at = row["timestamp"] or now.strftime("%Y-%m-%dT%H:%M:%SZ")

    if max_q > critical_threshold:
        return Anomaly(
            type="BILLING_QUEUE_SPIKE",
            severity=AnomalySeverity.CRITICAL,
            description=f"Queue depth reached {max_q} at {detected_at} — threshold is {critical_threshold}",
            suggested_action="Open additional billing counter or redirect staff immediately",
            detected_at=detected_at,
        )
    if max_q > warn_threshold:
        return Anomaly(
            type="BILLING_QUEUE_SPIKE",
            severity=AnomalySeverity.WARN,
            description=f"Queue depth reached {max_q} at {detected_at} — threshold is {warn_threshold}",
            suggested_action="Open additional billing counter or redirect staff",
            detected_at=detected_at,
        )
    return None


async def _detect_conversion_drop(db, store_id: str, now: datetime) -> Optional[Anomaly]:
    drop_threshold = _conversion_drop_pct()

    # Day-0 behaviour: if the 7-day rolling average returns 0.0 (no historical data yet),
    # we skip the anomaly entirely rather than emitting a spurious CONVERSION_DROP on the
    # first day the store goes live. The check `if avg_rate == 0.0: return None` below handles this.

    # Today's conversion rate
    today_start = now.strftime("%Y-%m-%dT00:00:00Z")
    today_end = now.strftime("%Y-%m-%dT23:59:59Z")

    async def _conversion_rate_for_window(ts_start: str, ts_end: str) -> float:
        uv = await db.fetchone(
            "SELECT COUNT(DISTINCT visitor_id) AS cnt FROM events WHERE store_id=? AND event_type='ENTRY' AND is_staff=0 AND timestamp BETWEEN ? AND ?",
            (store_id, ts_start, ts_end),
        )
        bj = await db.fetchone(
            "SELECT COUNT(DISTINCT visitor_id) AS cnt FROM events WHERE store_id=? AND event_type='BILLING_QUEUE_JOIN' AND is_staff=0 AND timestamp BETWEEN ? AND ?",
            (store_id, ts_start, ts_end),
        )
        ba = await db.fetchone(
            "SELECT COUNT(DISTINCT visitor_id) AS cnt FROM events WHERE store_id=? AND event_type='BILLING_QUEUE_ABANDON' AND is_staff=0 AND timestamp BETWEEN ? AND ?",
            (store_id, ts_start, ts_end),
        )
        visitors = uv["cnt"] if uv else 0
        join_count = bj["cnt"] if bj else 0
        abandon_count = ba["cnt"] if ba else 0
        converted = max(0, join_count - abandon_count)
        return (converted / visitors) if visitors > 0 else 0.0

    today_rate = await _conversion_rate_for_window(today_start, today_end)

    # 7-day rolling average (excluding today)
    seven_days_ago = (now - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
    yesterday_end = (now - timedelta(days=1)).strftime("%Y-%m-%dT23:59:59Z")
    avg_rate = await _conversion_rate_for_window(seven_days_ago, yesterday_end)

    if avg_rate == 0.0:
        return None

    if today_rate < avg_rate * drop_threshold:
        today_pct = round(today_rate * 100, 1)
        avg_pct = round(avg_rate * 100, 1)
        return Anomaly(
            type="CONVERSION_DROP",
            severity=AnomalySeverity.WARN,
            description=f"Conversion rate {today_pct}% vs 7-day average {avg_pct}%",
            suggested_action="Check entry funnel stage or zone engagement for blockages",
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    return None


async def _detect_dead_zone(db, store_id: str, now: datetime) -> Optional[Anomaly]:
    dead_zone_mins = _dead_zone_minutes()
    window_start = (now - timedelta(minutes=dead_zone_mins)).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Only relevant during typical open hours (8am–10pm UTC proxy)
    hour = now.hour
    if not (8 <= hour < 22):
        return None

    zone_row = await db.fetchone(
        """
        SELECT COUNT(*) AS cnt
        FROM events
        WHERE store_id = ?
          AND event_type = 'ZONE_ENTER'
          AND is_staff = 0
          AND timestamp >= ?
        """,
        (store_id, window_start),
    )
    # Only trigger if store has had events overall (avoid false positives before pipeline runs)
    total_row = await db.fetchone(
        "SELECT COUNT(*) AS cnt FROM events WHERE store_id = ?",
        (store_id,),
    )
    total_events = total_row["cnt"] if total_row else 0
    if total_events == 0:
        return None

    zone_count = zone_row["cnt"] if zone_row else 0
    if zone_count == 0:
        return Anomaly(
            type="DEAD_ZONE",
            severity=AnomalySeverity.INFO,
            description=f"No zone visits recorded in the last {dead_zone_mins} minutes during open hours",
            suggested_action="Verify camera feeds are active and pipeline is processing",
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    return None


async def _detect_stale_feed(db, store_id: str, now: datetime) -> Optional[Anomaly]:
    stale_mins = _stale_feed_minutes()
    cutoff = (now - timedelta(minutes=stale_mins)).strftime("%Y-%m-%dT%H:%M:%SZ")

    row = await db.fetchone(
        """
        SELECT MAX(timestamp) AS last_ts
        FROM events
        WHERE store_id = ?
        """,
        (store_id,),
    )
    if not row or row["last_ts"] is None:
        return None

    last_ts = row["last_ts"]
    if last_ts < cutoff:
        return Anomaly(
            type="STALE_FEED",
            severity=AnomalySeverity.CRITICAL,
            description=f"No events received since {last_ts} — feed has been silent for over {stale_mins} minutes",
            suggested_action="Check camera connectivity and pipeline process health",
            detected_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    return None
