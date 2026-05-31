"""
Event schema validation and JSONL emission for the detection pipeline.
All events pass through Pydantic validation before being written; nothing is silently dropped.
"""

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator, ValidationError

logger = logging.getLogger(__name__)

VALID_EVENT_TYPES: frozenset[str] = frozenset({
    "ENTRY",
    "EXIT",
    "ZONE_ENTER",
    "ZONE_EXIT",
    "ZONE_DWELL",
    "BILLING_QUEUE_JOIN",
    "BILLING_QUEUE_ABANDON",
    "REENTRY",
})

CONF_LOW_THRESHOLD = 0.4


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = Field(..., ge=1)
    low_conf: bool = False


class StoreEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: str
    timestamp: str
    zone_id: Optional[str] = None
    dwell_ms: int = Field(default=0, ge=0)
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        if v not in VALID_EVENT_TYPES:
            raise ValueError(f"unknown event_type '{v}'; valid={VALID_EVENT_TYPES}")
        return v

    @field_validator("visitor_id")
    @classmethod
    def validate_visitor_id(cls, v: str) -> str:
        if not (v.startswith("VIS_") and len(v) == 10):
            raise ValueError(f"visitor_id must be VIS_<6hex>, got '{v}'")
        return v

    @field_validator("timestamp")
    @classmethod
    def validate_iso_timestamp(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

    @classmethod
    def build(
        cls,
        store_id: str,
        camera_id: str,
        visitor_id: str,
        event_type: str,
        timestamp: datetime,
        confidence: float,
        session_seq: int,
        zone_id: Optional[str] = None,
        dwell_ms: int = 0,
        is_staff: bool = False,
        queue_depth: Optional[int] = None,
        sku_zone: Optional[str] = None,
    ) -> "StoreEvent":
        ts_str = timestamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        return cls(
            event_id=str(uuid.uuid4()),
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=event_type,
            timestamp=ts_str,
            zone_id=zone_id,
            dwell_ms=dwell_ms,
            is_staff=is_staff,
            confidence=confidence,
            metadata=EventMetadata(
                queue_depth=queue_depth,
                sku_zone=sku_zone,
                session_seq=session_seq,
                low_conf=confidence < CONF_LOW_THRESHOLD,
            ),
        )


class EventEmitter:
    """Collects validated StoreEvents in memory and flushes them to JSONL."""

    def __init__(
        self,
        store_id: str,
        camera_id: str,
        clip_start_ts: datetime,
        fps: float,
    ) -> None:
        self._store_id = store_id
        self._camera_id = camera_id
        self._clip_start_ts = clip_start_ts
        self._fps = fps
        self._events: list[dict[str, Any]] = []
        self._rejected = 0

    @property
    def event_count(self) -> int:
        return len(self._events)

    @property
    def rejected_count(self) -> int:
        return self._rejected

    def frame_to_timestamp(self, frame_number: int) -> datetime:
        from datetime import timedelta
        offset = timedelta(seconds=frame_number / self._fps)
        return self._clip_start_ts + offset

    def emit(self, event: StoreEvent) -> None:
        try:
            validated = StoreEvent.model_validate(event.model_dump())
            self._events.append(validated.model_dump())
        except ValidationError as exc:
            self._rejected += 1
            logger.warning(
                "Event failed validation — excluded from output",
                extra={
                    "visitor_id": getattr(event, "visitor_id", "unknown"),
                    "event_type": getattr(event, "event_type", "unknown"),
                    "error": str(exc),
                },
            )

    def write(self, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as fh:
            for event_dict in self._events:
                fh.write(json.dumps(event_dict, default=str) + "\n")
        logger.info(
            "JSONL written",
            extra={
                "path": str(output_path),
                "accepted": len(self._events),
                "rejected": self._rejected,
            },
        )
