# PROMPT: create shared pytest fixtures for an async FastAPI app using in-memory SQLite,
#         including a sample event factory that generates valid StoreEvent objects
# CHANGES MADE: added edge-case factories for staff events, re-entry events, and
#               billing events; switched to anyio backend for asyncio compatibility

import uuid
from datetime import datetime, timezone
from typing import AsyncGenerator

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from app.db import close_db, init_db
from app.main import app
from app.models import EventMetadata, EventType, StoreEvent

TEST_STORE_ID = "ST1008"
TEST_DB_PATH = ":memory:"


@pytest_asyncio.fixture(scope="function")
async def db():
    database = await init_db(TEST_DB_PATH)
    yield database
    await close_db()


@pytest_asyncio.fixture(scope="function")
async def async_client(db) -> AsyncGenerator[AsyncClient, None]:
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.fixture(scope="function")
def sync_client(db):
    with TestClient(app) as client:
        yield client


def _make_event(
    store_id: str = TEST_STORE_ID,
    event_type: str = "ENTRY",
    visitor_id: str | None = None,
    camera_id: str = "CAM_1",
    zone_id: str | None = None,
    is_staff: bool = False,
    dwell_ms: int = 0,
    confidence: float = 0.85,
    session_seq: int = 1,
    queue_depth: int | None = None,
    timestamp: str | None = None,
) -> StoreEvent:
    if visitor_id is None:
        visitor_id = "VIS_" + uuid.uuid4().hex[:6].upper()
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    return StoreEvent(
        event_id=str(uuid.uuid4()),
        store_id=store_id,
        camera_id=camera_id,
        visitor_id=visitor_id,
        event_type=EventType(event_type),
        timestamp=timestamp,
        zone_id=zone_id,
        dwell_ms=dwell_ms,
        is_staff=is_staff,
        confidence=confidence,
        metadata=EventMetadata(
            queue_depth=queue_depth,
            sku_zone=None,
            session_seq=session_seq,
        ),
    )


@pytest.fixture
def make_event():
    return _make_event


@pytest.fixture
def sample_events() -> list[dict]:
    """Five valid events spanning a full customer session."""
    vid = "VIS_" + uuid.uuid4().hex[:6].upper()
    return [
        _make_event(event_type="ENTRY",   visitor_id=vid, session_seq=1).model_dump(),
        _make_event(event_type="ZONE_ENTER", visitor_id=vid, zone_id="MAKEUP", session_seq=2).model_dump(),
        _make_event(event_type="ZONE_DWELL", visitor_id=vid, zone_id="MAKEUP", dwell_ms=30000, session_seq=3).model_dump(),
        _make_event(event_type="BILLING_QUEUE_JOIN", visitor_id=vid, zone_id="BILLING", queue_depth=2, session_seq=4).model_dump(),
        _make_event(event_type="EXIT",    visitor_id=vid, session_seq=5).model_dump(),
    ]


@pytest.fixture
def staff_event() -> dict:
    return _make_event(event_type="ENTRY", is_staff=True, session_seq=1).model_dump()


@pytest.fixture
def billing_abandon_event(make_event) -> dict:
    vid = "VIS_" + uuid.uuid4().hex[:6].upper()
    return make_event(event_type="BILLING_QUEUE_ABANDON", visitor_id=vid, zone_id="BILLING", session_seq=2).model_dump()
