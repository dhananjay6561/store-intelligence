"""
Staff classifier using torso colour histogram + zone-frequency heuristic.
Staff wear uniforms; high zone traversal distinguishes them from customers.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np

logger = logging.getLogger(__name__)

# Staff zone-frequency threshold — more than this many distinct zones in one session = staff
STAFF_ZONE_COUNT_THRESHOLD = 4
# Bhattacharyya distance below which a histogram matches the uniform colour range
HISTOGRAM_DISTANCE_THRESHOLD = 0.35


@dataclass
class TrackHistory:
    track_id: int
    torso_histograms: list[np.ndarray] = field(default_factory=list)
    zones_visited: set[str] = field(default_factory=set)
    frame_count: int = 0


def _extract_torso_roi(frame: np.ndarray, bbox: tuple[float, float, float, float]) -> Optional[np.ndarray]:
    """Crop the torso region (middle third of bounding box height)."""
    x1, y1, x2, y2 = (int(v) for v in bbox)
    h = y2 - y1
    torso_y1 = y1 + h // 3
    torso_y2 = y1 + (2 * h) // 3
    if torso_y2 <= torso_y1 or x2 <= x1:
        return None
    roi = frame[torso_y1:torso_y2, x1:x2]
    if roi.size == 0:
        return None
    return roi


def _compute_hsv_histogram(roi: np.ndarray) -> Optional[np.ndarray]:
    try:
        import cv2
        hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
        hist = cv2.calcHist([hsv], [0, 1], None, [18, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
        return hist
    except Exception:
        return None


def _mean_histogram(histograms: list[np.ndarray]) -> Optional[np.ndarray]:
    if not histograms:
        return None
    stacked = np.stack(histograms, axis=0)
    return stacked.mean(axis=0)


def _uniform_reference_histogram(lower_hsv: list[int], upper_hsv: list[int]) -> Optional[np.ndarray]:
    """Build a synthetic reference histogram for the uniform colour range."""
    try:
        import cv2
        # Create a solid-colour patch in the uniform HSV range midpoint
        mid_h = (lower_hsv[0] + upper_hsv[0]) // 2
        mid_s = (lower_hsv[1] + upper_hsv[1]) // 2
        mid_v = (lower_hsv[2] + upper_hsv[2]) // 2
        patch = np.full((50, 50, 3), [mid_h, mid_s, mid_v], dtype=np.uint8)
        hist = cv2.calcHist([patch], [0, 1], None, [18, 16], [0, 180, 0, 256])
        cv2.normalize(hist, hist, alpha=0, beta=1, norm_type=cv2.NORM_MINMAX)
        return hist
    except Exception:
        return None


class StaffClassifier:
    def __init__(self, uniform_hsv_lower: list[int], uniform_hsv_upper: list[int]) -> None:
        self._lower = uniform_hsv_lower
        self._upper = uniform_hsv_upper
        self._reference_hist = _uniform_reference_histogram(uniform_hsv_lower, uniform_hsv_upper)

    def update_history(
        self,
        history: TrackHistory,
        frame: np.ndarray,
        bbox: tuple[float, float, float, float],
        zone_id: Optional[str],
    ) -> None:
        """Update track history with a new frame observation."""
        history.frame_count += 1
        if zone_id:
            history.zones_visited.add(zone_id)
        roi = _extract_torso_roi(frame, bbox)
        if roi is not None:
            hist = _compute_hsv_histogram(roi)
            if hist is not None and len(history.torso_histograms) < 30:
                history.torso_histograms.append(hist)

    def classify(self, history: TrackHistory) -> bool:
        """Return True if the track is classified as staff."""
        if len(history.zones_visited) > STAFF_ZONE_COUNT_THRESHOLD:
            logger.debug(
                "Staff via zone frequency",
                extra={"track_id": history.track_id, "zones": len(history.zones_visited)},
            )
            return True

        if history.torso_histograms and self._reference_hist is not None:
            mean_hist = _mean_histogram(history.torso_histograms)
            if mean_hist is not None:
                try:
                    import cv2
                    distance = cv2.compareHist(mean_hist, self._reference_hist, cv2.HISTCMP_BHATTACHARYYA)
                    if distance < HISTOGRAM_DISTANCE_THRESHOLD:
                        logger.debug(
                            "Staff via uniform colour match",
                            extra={"track_id": history.track_id, "bhattacharyya": round(distance, 3)},
                        )
                        return True
                except Exception:
                    pass

        return False
