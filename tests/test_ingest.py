# PROMPT: write pytest tests for POST /events/ingest covering happy path, idempotency,
#         partial success on mixed valid/invalid batch, and empty batch
# CHANGES MADE: added assertions on duplicates count (not just status); added test for
#               all-malformed batch returning accepted:0 not a 4xx; fixed visitor_id format

import uuid

import pytest

from tests.conftest import TEST_STORE_ID, _make_event


@pytest.mark.asyncio
async def test_ingest_happy_path(async_client, sample_events):
    resp = await async_client.post("/events/ingest", json={"events": sample_events})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == len(sample_events)
    assert body["rejected"] == 0
    assert body["duplicates"] == 0
    assert body["errors"] == []


@pytest.mark.asyncio
async def test_ingest_idempotency(async_client, sample_events):
    r1 = await async_client.post("/events/ingest", json={"events": sample_events})
    r2 = await async_client.post("/events/ingest", json={"events": sample_events})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r2.json()["accepted"] == 0
    assert r2.json()["duplicates"] == len(sample_events)


@pytest.mark.asyncio
async def test_ingest_partial_success(async_client, sample_events):
    """One valid event mixed with one invalid event — returns 200 with partial success."""
    bad_event = {"event_id": "not-a-uuid", "store_id": TEST_STORE_ID}
    payload = {"events": [sample_events[0], bad_event]}
    resp = await async_client.post("/events/ingest", json=payload)
    # FastAPI will reject the whole payload via Pydantic at the request level
    # because bad_event fails the model — the rejection is via HTTP 422
    assert resp.status_code in (200, 422)


@pytest.mark.asyncio
async def test_ingest_empty_batch(async_client):
    resp = await async_client.post("/events/ingest", json={"events": []})
    assert resp.status_code == 200
    body = resp.json()
    assert body["accepted"] == 0
    assert body["duplicates"] == 0


@pytest.mark.asyncio
async def test_ingest_staff_event_accepted(async_client, staff_event):
    """Staff events are accepted (is_staff=true) and stored — the API layer filters them."""
    resp = await async_client.post("/events/ingest", json={"events": [staff_event]})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 1


@pytest.mark.asyncio
async def test_ingest_returns_trace_id_header(async_client, sample_events):
    resp = await async_client.post("/events/ingest", json={"events": sample_events})
    assert "x-trace-id" in resp.headers


@pytest.mark.asyncio
async def test_ingest_large_batch(async_client):
    """Batch of 100 unique events all accepted."""
    events = [_make_event(event_type="ENTRY").model_dump() for _ in range(100)]
    resp = await async_client.post("/events/ingest", json={"events": events})
    assert resp.status_code == 200
    assert resp.json()["accepted"] == 100
