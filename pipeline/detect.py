"""
Main YOLO + ByteTrack detection orchestrator.

Processes a single video clip frame-by-frame:
  1. YOLOv8n detects person bounding boxes
  2. ByteTrack assigns persistent track_ids
  3. VisitorTracker maps track_ids to stable visitor_ids and emits events
  4. POSCorrelator marks billing-zone sessions as converted or abandoned
"""

import argparse
import json
import logging
import logging.config
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Clip filename pattern: any prefix followed by a date+time portion
# e.g. "CAM 1.mp4", "CAM_ENTRY_2026-04-10_08-00-00.mp4"
_TIMESTAMP_RE = re.compile(r"(\d{4}[-_]\d{2}[-_]\d{2})[T _-](\d{2}[-:]\d{2}[-:]\d{2})")
_DEFAULT_CLIP_TS = datetime(2026, 4, 10, 8, 0, 0, tzinfo=timezone.utc)

PERSON_CLASS_ID = 0
ENTRY_THRESHOLD_DIRECTION = "up"    # centroid moving upward = entering store


def _infer_clip_start_ts(clip_path: Path) -> datetime:
    """Extract recording timestamp from filename, fall back to a fixed sentinel."""
    match = _TIMESTAMP_RE.search(clip_path.stem)
    if match:
        date_part = match.group(1).replace("_", "-")
        time_part = match.group(2).replace("-", ":").replace("_", ":")
        try:
            ts = datetime.strptime(f"{date_part}T{time_part}", "%Y-%m-%dT%H:%M:%S")
            return ts.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return _DEFAULT_CLIP_TS


def _camera_id_from_filename(filename: str) -> str:
    """Derive a normalised camera_id from the clip filename."""
    stem = Path(filename).stem.upper().replace(" ", "_")
    clean = re.sub(r"[^A-Z0-9_]", "", stem)
    return clean if clean else "CAM_UNKNOWN"


def _compute_queue_depth(
    detections_in_billing: list,
    billing_polygon: Optional[list],
) -> int:
    """Count how many bounding-box centroids are inside the billing polygon."""
    if not billing_polygon or not detections_in_billing:
        return 0
    return len(detections_in_billing)


def _is_crossing_threshold(
    prev_y: Optional[float],
    curr_y: float,
    threshold_y: float,
    inbound_direction: str,
) -> bool:
    """Detect whether a centroid just crossed the entry threshold line."""
    if prev_y is None:
        return False
    if inbound_direction == "up":
        return prev_y >= threshold_y > curr_y
    return prev_y <= threshold_y < curr_y


def process_clip(
    clip_path: Path,
    store_layout: dict,
    pos_csv_path: Path,
    output_dir: Path,
) -> str:
    """Process one video clip, emit events, return store_id."""
    from ultralytics import YOLO
    import cv2
    import numpy as np

    from pipeline.emit import EventEmitter
    from pipeline.pos_correlator import POSCorrelator
    from pipeline.staff import StaffClassifier
    from pipeline.tracker import Detection, VisitorTracker
    from pipeline.zone import ZoneClassifier

    store_id: str = store_layout["store_id"]
    camera_id = _camera_id_from_filename(clip_path.name)
    clip_start_ts = _infer_clip_start_ts(clip_path)

    zone_classifier = ZoneClassifier(store_layout)
    uniform_cfg = store_layout.get("staff_uniform_hsv", {})
    staff_classifier = StaffClassifier(
        uniform_hsv_lower=uniform_cfg.get("lower", [100, 50, 50]),
        uniform_hsv_upper=uniform_cfg.get("upper", [130, 255, 255]),
    )

    cap = cv2.VideoCapture(str(clip_path))
    if not cap.isOpened():
        raise RuntimeError(f"Cannot open video: {clip_path}")

    fps: float = cap.get(cv2.CAP_PROP_FPS) or 15.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))

    emitter = EventEmitter(
        store_id=store_id,
        camera_id=camera_id,
        clip_start_ts=clip_start_ts,
        fps=fps,
    )

    tracker = VisitorTracker(
        zone_classifier=zone_classifier,
        staff_classifier=staff_classifier,
        fps=fps,
    )

    pos_correlator = POSCorrelator.from_csv(pos_csv_path, store_id)

    model = YOLO("yolov8n.pt")

    # Entry threshold from layout
    entry_cfg = store_layout.get("entry_threshold", {})
    threshold_line = entry_cfg.get("line", [[0, 900], [1920, 900]])
    threshold_y: float = threshold_line[0][1]
    inbound_direction: str = entry_cfg.get("inbound_direction", "up")

    prev_centroids: dict[int, float] = {}   # track_id → previous centroid_y

    frame_number = 0
    log_interval = max(1, total_frames // 20)

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        timestamp = emitter.frame_to_timestamp(frame_number)

        results = model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            classes=[PERSON_CLASS_ID],
            verbose=False,
            conf=0.25,
        )

        detections: list[Detection] = []

        if results and results[0].boxes is not None and results[0].boxes.id is not None:
            boxes = results[0].boxes
            for i in range(len(boxes)):
                track_id_t = boxes.id[i]
                if track_id_t is None:
                    continue
                track_id = int(track_id_t.item())
                conf = float(boxes.conf[i].item())
                xyxy = boxes.xyxy[i].cpu().numpy()
                x1, y1, x2, y2 = xyxy
                cx = float((x1 + x2) / 2)
                cy = float((y1 + y2) / 2)

                zone_id = zone_classifier.classify(cx, cy)

                det = Detection(
                    track_id=track_id,
                    bbox=(float(x1), float(y1), float(x2), float(y2)),
                    confidence=conf,
                    centroid=(cx, cy),
                    frame_number=frame_number,
                    timestamp=timestamp,
                    zone_id=zone_id,
                    queue_depth=0,   # updated below
                )
                detections.append(det)

        # Compute billing queue depth from centroids in billing zones
        billing_dets = [d for d in detections if zone_classifier.is_billing_zone(d.zone_id)]
        queue_depth = len(billing_dets)
        for det in billing_dets:
            det.queue_depth = queue_depth

        # Register billing entries for POS correlation
        for det in detections:
            if zone_classifier.is_billing_zone(det.zone_id):
                sess_id = tracker._track_to_visitor.get(det.track_id)
                if sess_id:
                    pos_correlator.record_billing_entry(
                        visitor_id=sess_id,
                        entry_ts=timestamp,
                        zone_id=det.zone_id or "BILLING",
                        session_seq=tracker._active.get(det.track_id, type("_", (), {"session_seq": 0})()).session_seq,  # noqa: E501
                    )

        tracker.update(
            detections=detections,
            frame_number=frame_number,
            fps=fps,
            clip_start_ts=clip_start_ts,
            emitter=emitter,
            store_id=store_id,
            camera_id=camera_id,
            frame=frame,
        )

        prev_centroids = {d.track_id: d.centroid[1] for d in detections}

        if frame_number % log_interval == 0:
            logger.info(
                "Processing",
                extra={
                    "clip": clip_path.name,
                    "frame": frame_number,
                    "total": total_frames,
                    "events_so_far": emitter.event_count,
                },
            )

        frame_number += 1

    cap.release()

    clip_end_ts = emitter.frame_to_timestamp(frame_number)
    tracker.finalize(
        final_frame=frame_number,
        fps=fps,
        clip_start_ts=clip_start_ts,
        emitter=emitter,
        store_id=store_id,
        camera_id=camera_id,
    )

    pos_correlator.correlate_and_emit(
        emitter=emitter,
        store_id=store_id,
        camera_id=camera_id,
        clip_end_ts=clip_end_ts,
    )

    output_path = output_dir / f"{store_id}_events.jsonl"
    emitter.write(output_path)

    logger.info(
        "Clip processed",
        extra={
            "clip": clip_path.name,
            "store_id": store_id,
            "frames": frame_number,
            "events": emitter.event_count,
            "rejected": emitter.rejected_count,
        },
    )
    return store_id


def _configure_logging(level: str) -> None:
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "logging.Formatter",
                "fmt": '{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
                "datefmt": "%Y-%m-%dT%H:%M:%S",
            }
        },
        "handlers": {
            "stdout": {
                "class": "logging.StreamHandler",
                "stream": "ext://sys.stdout",
                "formatter": "json",
            }
        },
        "root": {"level": level, "handlers": ["stdout"]},
    })


def main() -> None:
    parser = argparse.ArgumentParser(description="Process a CCTV clip and emit store events.")
    parser.add_argument("--clip", required=True, type=Path)
    parser.add_argument("--layout", required=True, type=Path)
    parser.add_argument("--pos", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    _configure_logging(args.log_level)

    with args.layout.open() as fh:
        store_layout = json.load(fh)

    store_id = process_clip(
        clip_path=args.clip,
        store_layout=store_layout,
        pos_csv_path=args.pos,
        output_dir=args.output,
    )
    print(store_id)


if __name__ == "__main__":
    main()
