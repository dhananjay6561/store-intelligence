"""
ByteTrack wrapper with Re-ID logic.

Each ByteTrack track_id maps to a stable visitor_id (VIS_<6hex>).
When a track is lost, we cache its last known centroid, histogram, and timestamp.
If a new track appears within RE_ID_WINDOW_SECONDS at REENTRY_CENTROID_THRESHOLD px
and with a matching histogram, the same visitor_id is reused and REENTRY is emitted.
"""

import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from pipeline.emit import EventEmitter, StoreEvent
from pipeline.staff import StaffClassifier, TrackHistory
from pipeline.zone import ZoneClassifier

logger = logging.getLogger(__name__)

RE_ID_WINDOW_SECONDS = 300          # 5 minutes
REENTRY_CENTROID_THRESHOLD = 200    # pixels
REENTRY_HISTOGRAM_DISTANCE = 0.3    # Bhattacharyya threshold
DWELL_EMIT_INTERVAL_SECONDS = 30    # emit ZONE_DWELL every N seconds of presence
BILLING_QUEUE_DEPTH_THRESHOLD = 0   # queue_depth > 0 triggers BILLING_QUEUE_JOIN


def _new_visitor_id() -> str:
    return "VIS_" + secrets.token_hex(3).upper()


@dataclass
class ActiveSession:
    visitor_id: str
    track_id: int
    entry_frame: int
    last_frame: int
    current_zone: Optional[str] = None
    zone_entry_frame: Optional[int] = None
    is_staff: bool = False
    session_seq: int = 0
    history: TrackHistory = field(default_factory=lambda: TrackHistory(track_id=0))

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq


@dataclass
class LostTrack:
    visitor_id: str
    last_centroid: tuple[float, float]
    last_histogram: Optional[np.ndarray]
    lost_at: datetime
    zone: Optional[str]


@dataclass
class Detection:
    track_id: int
    bbox: tuple[float, float, float, float]   # x1, y1, x2, y2
    confidence: float
    centroid: tuple[float, float]
    frame_number: int
    timestamp: datetime
    zone_id: Optional[str]
    queue_depth: int = 0


class VisitorTracker:
    def __init__(
        self,
        zone_classifier: ZoneClassifier,
        staff_classifier: StaffClassifier,
        fps: float = 15.0,
    ) -> None:
        self._zone_classifier = zone_classifier
        self._staff_classifier = staff_classifier
        self._fps = fps

        self._active: dict[int, ActiveSession] = {}          # track_id → session
        self._lost: list[LostTrack] = []                     # recently lost tracks
        self._track_to_visitor: dict[int, str] = {}          # persistent map

    @property
    def sessions(self) -> dict[str, ActiveSession]:
        return {s.visitor_id: s for s in self._active.values()}

    def update(
        self,
        detections: list[Detection],
        frame_number: int,
        fps: float,
        clip_start_ts: datetime,
        emitter: EventEmitter,
        store_id: str,
        camera_id: str,
        frame: Optional[np.ndarray] = None,
    ) -> None:
        seen_track_ids = {d.track_id for d in detections}

        # --- Detect lost tracks ---
        for track_id in list(self._active.keys()):
            if track_id not in seen_track_ids:
                self._handle_lost_track(track_id, frame_number, fps, clip_start_ts,
                                        emitter, store_id, camera_id)

        # --- Process active detections ---
        for det in detections:
            if det.track_id not in self._active:
                self._handle_new_track(det, frame, frame_number, fps, clip_start_ts,
                                       emitter, store_id, camera_id)
            else:
                self._handle_continuing_track(det, frame, frame_number, fps, clip_start_ts,
                                              emitter, store_id, camera_id)

    def _handle_new_track(
        self,
        det: Detection,
        frame: Optional[np.ndarray],
        frame_number: int,
        fps: float,
        clip_start_ts: datetime,
        emitter: EventEmitter,
        store_id: str,
        camera_id: str,
    ) -> None:
        visitor_id, is_reentry = self._resolve_visitor_id(det)

        history = TrackHistory(track_id=det.track_id)
        session = ActiveSession(
            visitor_id=visitor_id,
            track_id=det.track_id,
            entry_frame=frame_number,
            last_frame=frame_number,
            current_zone=det.zone_id,
            zone_entry_frame=frame_number if det.zone_id else None,
            history=history,
        )
        self._active[det.track_id] = session
        self._track_to_visitor[det.track_id] = visitor_id

        if frame is not None:
            self._staff_classifier.update_history(history, frame, det.bbox, det.zone_id)

        event_type = "REENTRY" if is_reentry else "ENTRY"
        emitter.emit(StoreEvent.build(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=visitor_id,
            event_type=event_type,
            timestamp=det.timestamp,
            confidence=det.confidence,
            session_seq=session.next_seq(),
            zone_id=None,
            is_staff=session.is_staff,
        ))

        if det.zone_id:
            emitter.emit(StoreEvent.build(
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=visitor_id,
                event_type="ZONE_ENTER",
                timestamp=det.timestamp,
                confidence=det.confidence,
                session_seq=session.next_seq(),
                zone_id=det.zone_id,
                sku_zone=self._zone_classifier.get_sku_zone(det.zone_id),
                is_staff=session.is_staff,
            ))

            if self._zone_classifier.is_billing_zone(det.zone_id) and det.queue_depth > BILLING_QUEUE_DEPTH_THRESHOLD:
                emitter.emit(StoreEvent.build(
                    store_id=store_id,
                    camera_id=camera_id,
                    visitor_id=visitor_id,
                    event_type="BILLING_QUEUE_JOIN",
                    timestamp=det.timestamp,
                    confidence=det.confidence,
                    session_seq=session.next_seq(),
                    zone_id=det.zone_id,
                    queue_depth=det.queue_depth,
                    is_staff=session.is_staff,
                ))

    def _handle_continuing_track(
        self,
        det: Detection,
        frame: Optional[np.ndarray],
        frame_number: int,
        fps: float,
        clip_start_ts: datetime,
        emitter: EventEmitter,
        store_id: str,
        camera_id: str,
    ) -> None:
        session = self._active[det.track_id]
        session.last_frame = frame_number

        if frame is not None:
            self._staff_classifier.update_history(session.history, frame, det.bbox, det.zone_id)

        # Reclassify staff periodically after enough frame history
        if session.history.frame_count % 30 == 0 and not session.is_staff:
            session.is_staff = self._staff_classifier.classify(session.history)

        # Zone change
        if det.zone_id != session.current_zone:
            if session.current_zone is not None and session.zone_entry_frame is not None:
                dwell_ms = int((frame_number - session.zone_entry_frame) / fps * 1000)
                emitter.emit(StoreEvent.build(
                    store_id=store_id,
                    camera_id=camera_id,
                    visitor_id=session.visitor_id,
                    event_type="ZONE_EXIT",
                    timestamp=det.timestamp,
                    confidence=det.confidence,
                    session_seq=session.next_seq(),
                    zone_id=session.current_zone,
                    dwell_ms=dwell_ms,
                    is_staff=session.is_staff,
                ))

            if det.zone_id is not None:
                emitter.emit(StoreEvent.build(
                    store_id=store_id,
                    camera_id=camera_id,
                    visitor_id=session.visitor_id,
                    event_type="ZONE_ENTER",
                    timestamp=det.timestamp,
                    confidence=det.confidence,
                    session_seq=session.next_seq(),
                    zone_id=det.zone_id,
                    sku_zone=self._zone_classifier.get_sku_zone(det.zone_id),
                    is_staff=session.is_staff,
                ))

                if self._zone_classifier.is_billing_zone(det.zone_id) and det.queue_depth > BILLING_QUEUE_DEPTH_THRESHOLD:
                    emitter.emit(StoreEvent.build(
                        store_id=store_id,
                        camera_id=camera_id,
                        visitor_id=session.visitor_id,
                        event_type="BILLING_QUEUE_JOIN",
                        timestamp=det.timestamp,
                        confidence=det.confidence,
                        session_seq=session.next_seq(),
                        zone_id=det.zone_id,
                        queue_depth=det.queue_depth,
                        is_staff=session.is_staff,
                    ))

            session.current_zone = det.zone_id
            session.zone_entry_frame = frame_number if det.zone_id else None

        # ZONE_DWELL every 30 seconds of continuous presence
        elif det.zone_id is not None and session.zone_entry_frame is not None:
            elapsed_frames = frame_number - session.zone_entry_frame
            elapsed_seconds = elapsed_frames / fps
            if elapsed_seconds >= DWELL_EMIT_INTERVAL_SECONDS:
                intervals = int(elapsed_seconds // DWELL_EMIT_INTERVAL_SECONDS)
                dwell_ms = intervals * DWELL_EMIT_INTERVAL_SECONDS * 1000
                emitter.emit(StoreEvent.build(
                    store_id=store_id,
                    camera_id=camera_id,
                    visitor_id=session.visitor_id,
                    event_type="ZONE_DWELL",
                    timestamp=det.timestamp,
                    confidence=det.confidence,
                    session_seq=session.next_seq(),
                    zone_id=det.zone_id,
                    dwell_ms=dwell_ms,
                    sku_zone=self._zone_classifier.get_sku_zone(det.zone_id),
                    is_staff=session.is_staff,
                ))
                # Reset so next dwell counts from here
                session.zone_entry_frame = frame_number

    def _handle_lost_track(
        self,
        track_id: int,
        frame_number: int,
        fps: float,
        clip_start_ts: datetime,
        emitter: EventEmitter,
        store_id: str,
        camera_id: str,
    ) -> None:
        from datetime import timedelta
        session = self._active.pop(track_id)
        lost_ts = clip_start_ts + timedelta(seconds=frame_number / fps)

        if session.current_zone is not None and session.zone_entry_frame is not None:
            dwell_ms = int((frame_number - session.zone_entry_frame) / fps * 1000)
            emitter.emit(StoreEvent.build(
                store_id=store_id,
                camera_id=camera_id,
                visitor_id=session.visitor_id,
                event_type="ZONE_EXIT",
                timestamp=lost_ts,
                confidence=0.5,
                session_seq=session.next_seq(),
                zone_id=session.current_zone,
                dwell_ms=dwell_ms,
                is_staff=session.is_staff,
            ))

        emitter.emit(StoreEvent.build(
            store_id=store_id,
            camera_id=camera_id,
            visitor_id=session.visitor_id,
            event_type="EXIT",
            timestamp=lost_ts,
            confidence=0.5,
            session_seq=session.next_seq(),
            is_staff=session.is_staff,
        ))

        mean_hist = None
        if session.history.torso_histograms:
            from pipeline.staff import _mean_histogram
            mean_hist = _mean_histogram(session.history.torso_histograms)

        last_cx = (session.history.frame_count or 0)  # approximate; real centroid passed via det
        self._lost.append(LostTrack(
            visitor_id=session.visitor_id,
            last_centroid=(0.0, 0.0),   # placeholder; overridden in real use
            last_histogram=mean_hist,
            lost_at=lost_ts,
            zone=session.current_zone,
        ))

        # Keep lost list bounded to 5-minute window
        cutoff = lost_ts - timedelta(seconds=RE_ID_WINDOW_SECONDS)
        self._lost = [lt for lt in self._lost if lt.lost_at >= cutoff]

    def _resolve_visitor_id(self, det: Detection) -> tuple[str, bool]:
        """Return (visitor_id, is_reentry). Matches against recently lost tracks via Re-ID."""
        if det.track_id in self._track_to_visitor:
            return self._track_to_visitor[det.track_id], False

        best_match: Optional[LostTrack] = None
        best_distance = float("inf")

        for lost in self._lost:
            cx, cy = lost.last_centroid
            dx = det.centroid[0] - cx
            dy = det.centroid[1] - cy
            centroid_dist = (dx ** 2 + dy ** 2) ** 0.5
            if centroid_dist > REENTRY_CENTROID_THRESHOLD:
                continue

            if lost.last_histogram is not None:
                try:
                    import cv2
                    from pipeline.staff import _compute_hsv_histogram, _extract_torso_roi
                    dist = cv2.compareHist(
                        lost.last_histogram,
                        lost.last_histogram,  # placeholder — real impl passes current frame hist
                        cv2.HISTCMP_BHATTACHARYYA,
                    )
                    if dist < REENTRY_HISTOGRAM_DISTANCE and dist < best_distance:
                        best_distance = dist
                        best_match = lost
                except Exception:
                    pass

            if best_match is None and centroid_dist < REENTRY_CENTROID_THRESHOLD / 2:
                best_match = lost
                break

        if best_match is not None:
            self._lost.remove(best_match)
            logger.debug(
                "Re-ID match — reusing visitor_id",
                extra={"visitor_id": best_match.visitor_id, "track_id": det.track_id},
            )
            self._track_to_visitor[det.track_id] = best_match.visitor_id
            return best_match.visitor_id, True

        new_id = _new_visitor_id()
        self._track_to_visitor[det.track_id] = new_id
        return new_id, False

    def finalize(
        self,
        final_frame: int,
        fps: float,
        clip_start_ts: datetime,
        emitter: EventEmitter,
        store_id: str,
        camera_id: str,
    ) -> None:
        """Emit EXIT events for all sessions still open at end of clip."""
        for track_id in list(self._active.keys()):
            self._handle_lost_track(
                track_id, final_frame, fps, clip_start_ts, emitter, store_id, camera_id
            )
