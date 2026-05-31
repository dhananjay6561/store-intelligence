"""
Pydantic v2 schemas for all inbound events and outbound API responses.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field, field_validator


class EventType(str, Enum):
    ENTRY = "ENTRY"
    EXIT = "EXIT"
    ZONE_ENTER = "ZONE_ENTER"
    ZONE_EXIT = "ZONE_EXIT"
    ZONE_DWELL = "ZONE_DWELL"
    BILLING_QUEUE_JOIN = "BILLING_QUEUE_JOIN"
    BILLING_QUEUE_ABANDON = "BILLING_QUEUE_ABANDON"
    REENTRY = "REENTRY"


class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: int = Field(..., ge=1)
    low_conf: bool = False


class StoreEvent(BaseModel):
    event_id: str
    store_id: str
    camera_id: str
    visitor_id: str
    event_type: EventType
    timestamp: str
    zone_id: Optional[str] = None
    dwell_ms: int = Field(default=0, ge=0)
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata

    @field_validator("timestamp")
    @classmethod
    def validate_iso_timestamp(cls, v: str) -> str:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

    @field_validator("visitor_id")
    @classmethod
    def validate_visitor_id_format(cls, v: str) -> str:
        if not (v.startswith("VIS_") and len(v) == 10):
            raise ValueError(f"visitor_id must be VIS_<6hex>, got '{v}'")
        return v


# --- Ingest ---

class IngestRequest(BaseModel):
    events: list[StoreEvent] = Field(..., max_length=500)


class IngestError(BaseModel):
    index: int
    event_id: Optional[str] = None
    reason: str


class IngestResponse(BaseModel):
    accepted: int
    rejected: int
    duplicates: int
    errors: list[IngestError]


# --- Metrics ---

class MetricsResponse(BaseModel):
    store_id: str
    window: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_by_zone: dict[str, float]
    queue_depth_current: int
    abandonment_rate: float
    generated_at: str


# --- Funnel ---

class FunnelStage(BaseModel):
    stage: str
    sessions: int
    drop_off_pct: float


class FunnelResponse(BaseModel):
    store_id: str
    funnel: list[FunnelStage]


# --- Heatmap ---

class ZoneHeatmapEntry(BaseModel):
    zone_id: str
    visit_count: int
    avg_dwell_ms: float
    score: int
    data_confidence: str


class HeatmapResponse(BaseModel):
    store_id: str
    zones: list[ZoneHeatmapEntry]


# --- Anomalies ---

class AnomalySeverity(str, Enum):
    INFO = "INFO"
    WARN = "WARN"
    CRITICAL = "CRITICAL"


class Anomaly(BaseModel):
    type: str
    severity: AnomalySeverity
    description: str
    suggested_action: str
    detected_at: str


class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[Anomaly]


# --- Health ---

class StoreHealthInfo(BaseModel):
    last_event_ts: Optional[str]
    feed_status: str
    event_count_today: int


class HealthResponse(BaseModel):
    status: str
    stores: dict[str, StoreHealthInfo]
    db_status: str
    uptime_seconds: float
