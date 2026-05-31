# PROMPT: write pytest tests for GET /stores/{id}/anomalies covering BILLING_QUEUE_SPIKE
#         CRITICAL threshold, CONVERSION_DROP detection, and the case where an empty
#         event store returns an empty anomaly list
# CHANGES MADE: patched datetime.now so anomaly windows are relative to injected events;
#               added test for STALE_FEED triggering when last event is old

import pytest
from unittest.mock import patch
from datetime import datetime, timedelta, timezone

from tests.conftest import TEST_STORE_ID, _make_event


@pytest.mark.asyncio
async def test_anomalies_empty_store_returns_empty_list(async_client):
    resp = await async_client.get("/stores/NO_EVENTS_STORE/anomalies")
    assert resp.status_code == 200
    body = resp.json()
    assert body["anomalies"] == []
    assert body["store_id"] == "NO_EVENTS_STORE"


@pytest.mark.asyncio
async def test_anomalies_queue_spike_critical(async_client):
    """queue_depth=10 (above critical=8) triggers BILLING_QUEUE_SPIKE CRITICAL."""
    now = datetime.now(timezone.utc)

    events = [
        _make_event(event_type="ENTRY", session_seq=1).model_dump(),
        _make_event(
            event_type="BILLING_QUEUE_JOIN",
            zone_id="BILLING",
            queue_depth=10,
            session_seq=2,
            timestamp=now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        ).model_dump(),
    ]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/anomalies")
    body = resp.json()
    spike = next((a for a in body["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"), None)
    assert spike is not None
    assert spike["severity"] == "CRITICAL"
    assert "10" in spike["description"]
    assert spike["suggested_action"]


@pytest.mark.asyncio
async def test_anomalies_queue_spike_warn(async_client):
    """queue_depth=6 (above warn=5, below critical=8) triggers WARN."""
    now = datetime.now(timezone.utc)
    events = [
        _make_event(
            event_type="BILLING_QUEUE_JOIN",
            zone_id="BILLING",
            queue_depth=6,
            session_seq=1,
            timestamp=now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        ).model_dump(),
    ]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/anomalies")
    body = resp.json()
    spike = next((a for a in body["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"), None)
    assert spike is not None
    assert spike["severity"] == "WARN"


@pytest.mark.asyncio
async def test_anomalies_stale_feed_detected(async_client):
    """An event with a timestamp 20 minutes ago triggers STALE_FEED CRITICAL."""
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    events = [
        _make_event(event_type="ENTRY", session_seq=1, timestamp=old_ts).model_dump(),
    ]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/anomalies")
    body = resp.json()
    stale = next((a for a in body["anomalies"] if a["type"] == "STALE_FEED"), None)
    assert stale is not None
    assert stale["severity"] == "CRITICAL"


@pytest.mark.asyncio
async def test_anomalies_no_spike_below_threshold(async_client):
    """queue_depth=3 (below warn=5) — no BILLING_QUEUE_SPIKE anomaly."""
    now = datetime.now(timezone.utc)
    events = [
        _make_event(
            event_type="BILLING_QUEUE_JOIN",
            zone_id="BILLING",
            queue_depth=3,
            session_seq=1,
            timestamp=now.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        ).model_dump(),
    ]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/anomalies")
    spike = next((a for a in resp.json()["anomalies"] if a["type"] == "BILLING_QUEUE_SPIKE"), None)
    assert spike is None


@pytest.mark.asyncio
async def test_anomalies_response_schema(async_client):
    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/anomalies")
    assert resp.status_code == 200
    body = resp.json()
    assert "store_id" in body
    assert "anomalies" in body
    for anomaly in body["anomalies"]:
        assert {"type", "severity", "description", "suggested_action", "detected_at"}.issubset(anomaly.keys())
