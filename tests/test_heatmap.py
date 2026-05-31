# PROMPT: write pytest tests for GET /stores/{id}/heatmap verifying score normalisation,
#         data_confidence LOW for < 20 sessions, and all zones from layout appear at 0 visits
# CHANGES MADE: used tmp layout file for zone list loading; added score range assertion

import json
import os
import pytest

from tests.conftest import TEST_STORE_ID, _make_event


@pytest.mark.asyncio
async def test_heatmap_response_schema(async_client):
    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/heatmap?window=all")
    assert resp.status_code == 200
    body = resp.json()
    assert "store_id" in body
    assert "zones" in body
    assert body["store_id"] == TEST_STORE_ID


@pytest.mark.asyncio
async def test_heatmap_score_normalised_0_to_100(async_client):
    """Max-visit zone must score 100; all scores in [0, 100]."""
    vid1 = "VIS_AA0001"
    vid2 = "VIS_BB0002"
    events = []
    for vid in [vid1, vid2]:
        events.append(_make_event(event_type="ENTRY", visitor_id=vid, session_seq=1).model_dump())
        events.append(_make_event(event_type="ZONE_ENTER", visitor_id=vid, zone_id="MAKEUP", session_seq=2).model_dump())

    # Only one visitor goes to SKINCARE
    events.append(_make_event(event_type="ZONE_ENTER", visitor_id=vid1, zone_id="SKINCARE", session_seq=3).model_dump())

    await async_client.post("/events/ingest", json={"events": events})
    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/heatmap?window=all")
    body = resp.json()

    scores = [z["score"] for z in body["zones"]]
    assert all(0 <= s <= 100 for s in scores), f"Scores out of range: {scores}"
    if scores:
        assert max(scores) == 100


@pytest.mark.asyncio
async def test_heatmap_data_confidence_low_when_few_sessions(async_client):
    """With fewer than 20 sessions, all zones return data_confidence=LOW."""
    event = _make_event(event_type="ENTRY", session_seq=1).model_dump()
    await async_client.post("/events/ingest", json={"events": [event]})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/heatmap?window=all")
    body = resp.json()
    for zone in body["zones"]:
        assert zone["data_confidence"] == "LOW"


@pytest.mark.asyncio
async def test_heatmap_data_confidence_high_when_enough_sessions(async_client):
    """With 20+ sessions, data_confidence should be HIGH."""
    events = [_make_event(event_type="ENTRY", session_seq=1).model_dump() for _ in range(20)]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/heatmap?window=all")
    body = resp.json()
    confidences = {z["data_confidence"] for z in body["zones"]}
    assert "HIGH" in confidences


@pytest.mark.asyncio
async def test_heatmap_zones_from_layout_present(async_client, tmp_path, monkeypatch):
    """Zones from layout file appear even with zero visits."""
    layout = {
        "store_id": TEST_STORE_ID,
        "zones": [
            {"zone_id": "TESTZONE_A", "label": "A", "polygon": [[0,0],[100,0],[100,100],[0,100]]},
            {"zone_id": "TESTZONE_B", "label": "B", "polygon": [[200,0],[300,0],[300,100],[200,100]]},
        ],
    }
    layout_file = tmp_path / "layout.json"
    layout_file.write_text(json.dumps(layout))
    monkeypatch.setenv("STORE_LAYOUT_PATH", str(layout_file))

    # Clear layout cache
    from app.heatmap import _LAYOUT_CACHE
    _LAYOUT_CACHE.clear()

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/heatmap?window=all")
    body = resp.json()
    zone_ids = {z["zone_id"] for z in body["zones"]}
    assert "TESTZONE_A" in zone_ids
    assert "TESTZONE_B" in zone_ids


@pytest.mark.asyncio
async def test_heatmap_zero_visits_zone_has_score_zero(async_client, tmp_path, monkeypatch):
    layout = {
        "store_id": TEST_STORE_ID,
        "zones": [
            {"zone_id": "VISITED_ZONE",   "label": "V", "polygon": [[0,0],[100,0],[100,100],[0,100]]},
            {"zone_id": "UNVISITED_ZONE", "label": "U", "polygon": [[200,0],[300,0],[300,100],[200,100]]},
        ],
    }
    layout_file = tmp_path / "layout2.json"
    layout_file.write_text(json.dumps(layout))
    monkeypatch.setenv("STORE_LAYOUT_PATH", str(layout_file))

    from app.heatmap import _LAYOUT_CACHE
    _LAYOUT_CACHE.clear()

    events = [
        _make_event(event_type="ENTRY", session_seq=1).model_dump(),
        _make_event(event_type="ZONE_ENTER", zone_id="VISITED_ZONE", session_seq=2).model_dump(),
    ]
    await async_client.post("/events/ingest", json={"events": events})

    resp = await async_client.get(f"/stores/{TEST_STORE_ID}/heatmap?window=all")
    body = resp.json()
    zone_map = {z["zone_id"]: z for z in body["zones"]}
    assert zone_map["UNVISITED_ZONE"]["visit_count"] == 0
    assert zone_map["UNVISITED_ZONE"]["score"] == 0
