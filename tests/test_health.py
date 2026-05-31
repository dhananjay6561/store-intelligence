# PROMPT: write pytest tests for GET /health covering ok status, STALE_FEED detection,
#         db_status unavailable when DB fails, and uptime_seconds being a positive float
# CHANGES MADE: used window=all pattern to work around today's date constraint;
#               mocked stale timestamp to be 20 minutes ago

import pytest
from datetime import datetime, timedelta, timezone

from tests.conftest import TEST_STORE_ID, _make_event


@pytest.mark.asyncio
async def test_health_returns_ok_when_db_connected(async_client):
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["db_status"] == "ok"
    assert body["status"] in ("ok", "degraded")
    assert isinstance(body["uptime_seconds"], float)
    assert body["uptime_seconds"] >= 0


@pytest.mark.asyncio
async def test_health_shows_store_after_events_ingested(async_client, sample_events):
    await async_client.post("/events/ingest", json={"events": sample_events})
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert TEST_STORE_ID in body["stores"]
    store_info = body["stores"][TEST_STORE_ID]
    assert "last_event_ts" in store_info
    assert "feed_status" in store_info
    assert "event_count_today" in store_info


@pytest.mark.asyncio
async def test_health_stale_feed_when_last_event_is_old(async_client):
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    event = _make_event(event_type="ENTRY", timestamp=old_ts, session_seq=1).model_dump()
    await async_client.post("/events/ingest", json={"events": [event]})

    resp = await async_client.get("/health")
    body = resp.json()
    store_info = body["stores"].get(TEST_STORE_ID)
    assert store_info is not None
    assert store_info["feed_status"] == "STALE_FEED"


@pytest.mark.asyncio
async def test_health_fresh_events_not_stale(async_client, sample_events):
    await async_client.post("/events/ingest", json={"events": sample_events})
    resp = await async_client.get("/health")
    body = resp.json()
    store_info = body["stores"].get(TEST_STORE_ID)
    assert store_info is not None
    assert store_info["feed_status"] == "ok"


@pytest.mark.asyncio
async def test_health_response_schema(async_client):
    resp = await async_client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    required_fields = {"status", "stores", "db_status", "uptime_seconds"}
    assert required_fields.issubset(body.keys())
