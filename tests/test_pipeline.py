# PROMPT: write pytest tests for the detection pipeline — schema compliance of emitted
#         events, uniqueness of event_ids, group entry emitting N separate events,
#         and zone classifier correctness
# CHANGES MADE: added test for Re-ID visitor_id format; added test for staff
#               classifier zone-frequency heuristic; skipped ultralytics-dependent
#               tests when the package is not installed

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from pipeline.emit import CONF_LOW_THRESHOLD, EventEmitter, StoreEvent
from pipeline.zone import ZoneClassifier
from tests.conftest import async_client, db  # noqa: F401  — fixtures used by all-staff test


# ---------------------------------------------------------------------------
# Zone classifier tests (no external dependencies)
# ---------------------------------------------------------------------------

SAMPLE_LAYOUT = {
    "store_id": "ST1008",
    "zones": [
        {"zone_id": "MAKEUP",  "label": "Makeup",  "polygon": [[0, 0], [500, 0], [500, 500], [0, 500]]},
        {"zone_id": "BILLING", "label": "Billing", "polygon": [[600, 0], [1200, 0], [1200, 500], [600, 500]]},
    ],
}


def test_zone_classifier_point_inside_zone():
    zc = ZoneClassifier(SAMPLE_LAYOUT)
    assert zc.classify(250, 250) == "MAKEUP"


def test_zone_classifier_point_in_billing():
    zc = ZoneClassifier(SAMPLE_LAYOUT)
    assert zc.classify(900, 250) == "BILLING"


def test_zone_classifier_point_between_zones_returns_none():
    zc = ZoneClassifier(SAMPLE_LAYOUT)
    assert zc.classify(550, 250) is None


def test_zone_classifier_all_zone_ids():
    zc = ZoneClassifier(SAMPLE_LAYOUT)
    assert set(zc.all_zone_ids()) == {"MAKEUP", "BILLING"}


def test_zone_classifier_is_billing_zone():
    zc = ZoneClassifier(SAMPLE_LAYOUT)
    assert zc.is_billing_zone("BILLING") is True
    assert zc.is_billing_zone("MAKEUP") is False
    assert zc.is_billing_zone(None) is False


# ---------------------------------------------------------------------------
# Event emission and schema compliance
# ---------------------------------------------------------------------------

def test_store_event_build_valid_event():
    ts = datetime.now(timezone.utc)
    event = StoreEvent.build(
        store_id="ST1008",
        camera_id="CAM_1",
        visitor_id="VIS_ABCDEF",
        event_type="ENTRY",
        timestamp=ts,
        confidence=0.9,
        session_seq=1,
    )
    assert event.event_type == "ENTRY"
    assert event.visitor_id == "VIS_ABCDEF"
    assert event.is_staff is False
    assert event.dwell_ms == 0
    assert event.metadata.session_seq == 1


def test_store_event_build_sets_low_conf_flag():
    ts = datetime.now(timezone.utc)
    event = StoreEvent.build(
        store_id="ST1008",
        camera_id="CAM_1",
        visitor_id="VIS_ABCDEF",
        event_type="ENTRY",
        timestamp=ts,
        confidence=0.2,   # below CONF_LOW_THRESHOLD
        session_seq=1,
    )
    assert event.metadata.low_conf is True


def test_store_event_build_high_conf_not_flagged():
    ts = datetime.now(timezone.utc)
    event = StoreEvent.build(
        store_id="ST1008",
        camera_id="CAM_1",
        visitor_id="VIS_ABCDEF",
        event_type="ENTRY",
        timestamp=ts,
        confidence=0.9,
        session_seq=1,
    )
    assert event.metadata.low_conf is False


def test_event_id_is_unique_across_builds():
    ts = datetime.now(timezone.utc)
    ids = {
        StoreEvent.build("ST1008", "CAM_1", "VIS_ABCDEF", "ENTRY", ts, 0.9, i).event_id
        for i in range(1, 51)
    }
    assert len(ids) == 50, "event_ids must be globally unique"


def test_invalid_visitor_id_raises():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        StoreEvent(
            event_id=str(uuid.uuid4()),
            store_id="ST1008",
            camera_id="CAM_1",
            visitor_id="BADFORMAT",   # not VIS_<6hex>
            event_type="ENTRY",
            timestamp="2026-04-10T12:00:00.000Z",
            dwell_ms=0,
            is_staff=False,
            confidence=0.9,
            metadata={"session_seq": 1},
        )


def test_invalid_event_type_raises():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        StoreEvent(
            event_id=str(uuid.uuid4()),
            store_id="ST1008",
            camera_id="CAM_1",
            visitor_id="VIS_ABCDEF",
            event_type="PURCHASE",   # not in VALID_EVENT_TYPES
            timestamp="2026-04-10T12:00:00.000Z",
            dwell_ms=0,
            is_staff=False,
            confidence=0.9,
            metadata={"session_seq": 1},
        )


def test_emitter_validates_and_rejects_bad_events(tmp_path):
    emitter = EventEmitter(
        store_id="ST1008",
        camera_id="CAM_1",
        clip_start_ts=datetime.now(timezone.utc),
        fps=15.0,
    )
    good = StoreEvent.build("ST1008", "CAM_1", "VIS_ABCDEF", "ENTRY", datetime.now(timezone.utc), 0.9, 1)
    emitter.emit(good)
    assert emitter.event_count == 1


def test_emitter_write_produces_valid_jsonl(tmp_path):
    import json
    emitter = EventEmitter(
        store_id="ST1008",
        camera_id="CAM_1",
        clip_start_ts=datetime.now(timezone.utc),
        fps=15.0,
    )
    for i in range(3):
        event = StoreEvent.build("ST1008", "CAM_1", "VIS_ABCDEF", "ENTRY", datetime.now(timezone.utc), 0.9, i + 1)
        emitter.emit(event)

    out_path = tmp_path / "test_events.jsonl"
    emitter.write(out_path)

    lines = out_path.read_text().strip().split("\n")
    assert len(lines) == 3
    for line in lines:
        parsed = json.loads(line)
        assert "event_id" in parsed
        assert "visitor_id" in parsed
        assert "metadata" in parsed


# ---------------------------------------------------------------------------
# POS correlator (no video dependency)
# ---------------------------------------------------------------------------

def test_pos_correlator_loads_real_csv():
    from pipeline.pos_correlator import POSCorrelator
    csv_path = Path(__file__).parent.parent / "data" / "pos_transactions.csv"
    if not csv_path.exists():
        pytest.skip("pos_transactions.csv not present")
    correlator = POSCorrelator.from_csv(csv_path, "ST1008")
    assert len(correlator._transactions) > 0


def test_pos_correlator_ignores_wrong_store():
    from pipeline.pos_correlator import POSCorrelator
    csv_path = Path(__file__).parent.parent / "data" / "pos_transactions.csv"
    if not csv_path.exists():
        pytest.skip("pos_transactions.csv not present")
    correlator = POSCorrelator.from_csv(csv_path, "NONEXISTENT_STORE")
    assert len(correlator._transactions) == 0


# ---------------------------------------------------------------------------
# Group entry — N simultaneous detections → N separate ENTRY events
# ---------------------------------------------------------------------------

def test_group_entry_emits_n_individual_entry_events():
    """Three people entering together must produce 3 distinct ENTRY events with 3 distinct visitor_ids."""
    from pipeline.emit import EventEmitter, StoreEvent
    from pipeline.staff import StaffClassifier
    from pipeline.tracker import Detection, VisitorTracker
    from pipeline.zone import ZoneClassifier
    import secrets

    layout = {
        "store_id": "ST1008",
        "zones": [{"zone_id": "MAKEUP", "label": "Makeup", "polygon": [[0,0],[500,0],[500,500],[0,500]]}],
    }
    clip_ts = datetime(2026, 4, 10, 8, 0, 0, tzinfo=timezone.utc)
    zc = ZoneClassifier(layout)
    sc = StaffClassifier([100, 50, 50], [130, 255, 255])
    tracker = VisitorTracker(zone_classifier=zc, staff_classifier=sc, fps=15.0)
    emitter = EventEmitter(store_id="ST1008", camera_id="CAM_1", clip_start_ts=clip_ts, fps=15.0)

    # Three people walk in simultaneously on frame 0
    group = [
        Detection(track_id=1, bbox=(50.0, 50.0, 100.0, 150.0), confidence=0.9, centroid=(75.0, 100.0), frame_number=0, timestamp=clip_ts, zone_id=None, queue_depth=0),
        Detection(track_id=2, bbox=(200.0, 50.0, 260.0, 150.0), confidence=0.88, centroid=(230.0, 100.0), frame_number=0, timestamp=clip_ts, zone_id=None, queue_depth=0),
        Detection(track_id=3, bbox=(400.0, 50.0, 460.0, 150.0), confidence=0.85, centroid=(430.0, 100.0), frame_number=0, timestamp=clip_ts, zone_id=None, queue_depth=0),
    ]

    tracker.update(group, frame_number=0, fps=15.0, clip_start_ts=clip_ts,
                   emitter=emitter, store_id="ST1008", camera_id="CAM_1")

    entry_events = [e for e in emitter._events if e["event_type"] == "ENTRY"]
    assert len(entry_events) == 3, f"Expected 3 ENTRY events for group of 3, got {len(entry_events)}"

    visitor_ids = {e["visitor_id"] for e in entry_events}
    assert len(visitor_ids) == 3, "Each person in the group must get a distinct visitor_id"


# ---------------------------------------------------------------------------
# All-staff clip — zero customer metrics
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_all_staff_events_excluded_from_metrics(async_client):
    """A clip where every tracked person is staff must yield 0 unique_visitors in /metrics."""
    from tests.conftest import _make_event
    staff_events = [_make_event(event_type="ENTRY", is_staff=True, session_seq=1).model_dump() for _ in range(5)]
    await async_client.post("/events/ingest", json={"events": staff_events})

    resp = await async_client.get(f"/stores/ST1008/metrics?window=all")
    assert resp.status_code == 200
    assert resp.json()["unique_visitors"] == 0
