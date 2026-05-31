"""
Zone classifier — deterministic polygon hit-test against store_layout.json.
No ML model required; centroid coordinate tested against each zone's polygon.
"""

import json
from pathlib import Path
from typing import Optional


def _point_in_polygon(px: float, py: float, polygon: list[list[float]]) -> bool:
    """Ray-casting algorithm for point-in-polygon test."""
    n = len(polygon)
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i]
        xj, yj = polygon[j]
        if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


class ZoneClassifier:
    def __init__(self, store_layout: dict) -> None:
        self._zones = store_layout.get("zones", [])

    @classmethod
    def from_file(cls, layout_path: Path) -> "ZoneClassifier":
        with layout_path.open(encoding="utf-8") as fh:
            layout = json.load(fh)
        return cls(layout)

    def classify(self, centroid_x: float, centroid_y: float) -> Optional[str]:
        """Return the zone_id for the given centroid, or None if between zones."""
        for zone in self._zones:
            polygon = zone["polygon"]
            if _point_in_polygon(centroid_x, centroid_y, polygon):
                return zone["zone_id"]
        return None

    def get_sku_zone(self, zone_id: str) -> Optional[str]:
        """Return the display label for a zone_id."""
        for zone in self._zones:
            if zone["zone_id"] == zone_id:
                return zone.get("label")
        return None

    def all_zone_ids(self) -> list[str]:
        return [z["zone_id"] for z in self._zones]

    def is_billing_zone(self, zone_id: Optional[str]) -> bool:
        return zone_id in {"BILLING", "QUEUE_AREA"}
