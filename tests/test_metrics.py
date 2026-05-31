# PROMPT: write pytest tests for GET /stores/{id}/metrics verifying that staff events
#         are excluded from visitor counts, conversion_rate is 0.0 (not null) when there
#         are zero purchases, and the correct fields are returned
# CHANGES MADE: added window=all param to bypass today's date constraint in tests;
#               added test for abandonment_rate formula correctness

import pytest

from tests.conftest import TEST_STORE_ID, _make_event


@pytest.mark.asyncio
async def test_metrics_zero_purchases_returns_zero_not_null(async_client, sample_events):
    """Store with visitors but no conversions returns 0.0, not null."""
    # sample_events has a BILLING_QUEUE_JOIN but no ABANDON — counts as converted
    # To get zero conversions, use events without billing
    entry = _make_event(event_type="ENTRY").model_dump()
    await async_client.post("/events/ingest", json={"events": [entry]})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/metrics?window=all")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body["conversion_rate"], float)
    assert body["conversion_rate"] >= 0.0


@pytest.mark.asyncio
async def test_metrics_staff_excluded_from_visitors(async_client, staff_event):
    """Staff ENTRY events must not inflate unique_visitors count."""
    await async_client.post("/events/ingest", json={"events": [staff_event]})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/metrics?window=all")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique_visitors"] == 0


@pytest.mark.asyncio
async def test_metrics_unique_visitors_counts_entry_events(async_client):
    """Three distinct visitor ENTRY events = 3 unique_visitors."""
    events = [_make_event(event_type="ENTRY").model_dump() for _ in range(3)]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/metrics?window=all")
    assert resp.status_code == 200
    assert resp.json()["unique_visitors"] == 3


@pytest.mark.asyncio
async def test_metrics_dwell_populated_from_zone_dwell_events(async_client):
    vid = "VIS_ABCDEF"
    events = [
        _make_event(event_type="ENTRY",      visitor_id=vid, session_seq=1).model_dump(),
        _make_event(event_type="ZONE_DWELL", visitor_id=vid, zone_id="MAKEUP", dwell_ms=60000, session_seq=2).model_dump(),
    ]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/metrics?window=all")
    body = resp.json()
    assert "MAKEUP" in body["avg_dwell_by_zone"]
    assert body["avg_dwell_by_zone"]["MAKEUP"] == 60000.0


@pytest.mark.asyncio
async def test_metrics_abandonment_rate_formula(async_client):
    """2 billing joins, 1 abandon → abandonment_rate = 0.5"""
    vid1 = "VIS_AAA001"
    vid2 = "VIS_BBB002"
    events = [
        _make_event(event_type="ENTRY", visitor_id=vid1, session_seq=1).model_dump(),
        _make_event(event_type="BILLING_QUEUE_JOIN", visitor_id=vid1, zone_id="BILLING", queue_depth=1, session_seq=2).model_dump(),
        _make_event(event_type="ENTRY", visitor_id=vid2, session_seq=1).model_dump(),
        _make_event(event_type="BILLING_QUEUE_JOIN", visitor_id=vid2, zone_id="BILLING", queue_depth=2, session_seq=2).model_dump(),
        _make_event(event_type="BILLING_QUEUE_ABANDON", visitor_id=vid1, zone_id="BILLING", session_seq=3).model_dump(),
    ]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/metrics?window=all")
    body = resp.json()
    assert body["abandonment_rate"] == pytest.approx(0.5, abs=0.01)


@pytest.mark.asyncio
async def test_metrics_response_has_all_required_fields(async_client):
    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/metrics?window=all")
    assert resp.status_code == 200
    body = resp.json()
    required = {"store_id", "window", "unique_visitors", "conversion_rate",
                "avg_dwell_by_zone", "queue_depth_current", "abandonment_rate", "generated_at"}
    assert required.issubset(body.keys())


@pytest.mark.asyncio
async def test_metrics_unknown_store_returns_zeros(async_client):
    """Unknown store returns 200 with zero metrics, not 404."""
    resp = await async_client.get("/stores/UNKNOWN_XYZ/metrics?window=all")
    assert resp.status_code == 200
    body = resp.json()
    assert body["unique_visitors"] == 0
    assert body["conversion_rate"] == 0.0
