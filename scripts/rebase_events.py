"""
Rebase event timestamps so the latest event lands at 'now' (UTC).

The bundled clip data is from 10-Apr-2026 and spans ~2.5 minutes. Run at
container startup to shift every timestamp forward by a constant delta, so the
historical footage reads as a fresh live feed regardless of when the deployed
link is opened. Relative spacing between events is preserved exactly.

Usage: python3 scripts/rebase_events.py <src.jsonl> <dst.jsonl>
"""

import json
import sys
from datetime import datetime, timezone
from pathlib import Path


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def main() -> None:
    src, dst = Path(sys.argv[1]), Path(sys.argv[2])
    events = [json.loads(line) for line in src.read_text().splitlines() if line.strip()]
    if not events:
        dst.write_text("")
        return

    latest = max(_parse(e["timestamp"]) for e in events)
    delta = datetime.now(timezone.utc) - latest
    for e in events:
        e["timestamp"] = _fmt(_parse(e["timestamp"]) + delta)

    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    print(f"rebased {len(events)} events; latest -> {events[-1]['timestamp']}")


if __name__ == "__main__":
    main()
