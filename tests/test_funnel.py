# PROMPT: write pytest tests for GET /stores/{id}/funnel verifying that REENTRY does
#         not double-count a visitor session, drop-off percentages sum correctly, and
#         an empty store returns a valid funnel with all zero stage counts
# CHANGES MADE: added test for drop-off math correctness; added window=all param

import pytest

from tests.conftest import TEST_STORE_ID, _make_event


@pytest.mark.asyncio
async def test_funnel_reentry_does_not_double_count(async_client):
    """One visitor with ENTRY + REENTRY counts as 1 session at ENTRY stage."""
    vid = "VIS_" + "re1234".upper()
    events = [
        _make_event(event_type="ENTRY",   visitor_id=vid, session_seq=1).model_dump(),
        _make_event(event_type="EXIT",    visitor_id=vid, session_seq=2).model_dump(),
        _make_event(event_type="REENTRY", visitor_id=vid, session_seq=3).model_dump(),
    ]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/funnel?window=all")
    assert resp.status_code == 200
    body = resp.json()
    entry_stage = next(s for s in body["funnel"] if s["stage"] == "ENTRY")
    assert entry_stage["sessions"] == 1


@pytest.mark.asyncio
async def test_funnel_drop_off_pct_math(async_client):
    """10 visitors enter, 8 visit a zone, 5 reach billing, 3 purchase → correct drop-offs."""
    from tests.conftest import _make_event
    import uuid

    events = []
    all_vids = ["VIS_" + uuid.uuid4().hex[:6].upper() for _ in range(10)]

    for vid in all_vids:
        events.append(_make_event(event_type="ENTRY", visitor_id=vid, session_seq=1).model_dump())

    for vid in all_vids[:8]:
        events.append(_make_event(event_type="ZONE_ENTER", visitor_id=vid, zone_id="MAKEUP", session_seq=2).model_dump())

    for vid in all_vids[:5]:
        events.append(_make_event(event_type="BILLING_QUEUE_JOIN", visitor_id=vid, zone_id="BILLING", queue_depth=1, session_seq=3).model_dump())

    # 2 abandon → 3 converted
    for vid in all_vids[:2]:
        events.append(_make_event(event_type="BILLING_QUEUE_ABANDON", visitor_id=vid, zone_id="BILLING", session_seq=4).model_dump())

    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/funnel?window=all")
    body = resp.json()
    stages = {s["stage"]: s for s in body["funnel"]}

    assert stages["ENTRY"]["sessions"] == 10
    assert stages["ZONE_VISIT"]["sessions"] == 8
    assert stages["BILLING_QUEUE"]["sessions"] == 5
    assert stages["PURCHASE"]["sessions"] == 3

    assert stages["ZONE_VISIT"]["drop_off_pct"] == pytest.approx(20.0, abs=0.5)
    assert stages["ENTRY"]["drop_off_pct"] == 0.0


@pytest.mark.asyncio
async def test_funnel_empty_store_returns_all_zeros(async_client):
    resp = await async_client.get("/stores/EMPTY_STORE/funnel?window=all")
    assert resp.status_code == 200
    body = resp.json()
    for stage in body["funnel"]:
        assert stage["sessions"] == 0
        assert stage["drop_off_pct"] == 0.0


@pytest.mark.asyncio
async def test_funnel_has_all_four_stages(async_client):
    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/funnel?window=all")
    assert resp.status_code == 200
    stages = {s["stage"] for s in resp.json()["funnel"]}
    assert stages == {"ENTRY", "ZONE_VISIT", "BILLING_QUEUE", "PURCHASE"}


@pytest.mark.asyncio
async def test_funnel_stages_monotonically_decreasing(async_client):
    """Each subsequent stage session count must be <= the previous one."""
    vid = "VIS_" + "f1b2c3".upper()
    events = [
        _make_event(event_type="ENTRY", visitor_id=vid, session_seq=1).model_dump(),
        _make_event(event_type="ZONE_ENTER", visitor_id=vid, zone_id="SKINCARE", session_seq=2).model_dump(),
        _make_event(event_type="BILLING_QUEUE_JOIN", visitor_id=vid, zone_id="BILLING", queue_depth=1, session_seq=3).model_dump(),
    ]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/funnel?window=all")
    counts = [s["sessions"] for s in resp.json()["funnel"]]
    for i in range(1, len(counts)):
        assert counts[i] <= counts[i - 1], f"Stage {i} ({counts[i]}) > stage {i-1} ({counts[i-1]})"
