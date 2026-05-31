"""
Replay JSONL event files into the API via POST /events/ingest.
Used by run.sh after pipeline processing to populate the database.
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

import httpx

logging.basicConfig(
    level=logging.INFO,
    format='{"ts":"%(asctime)s","level":"%(levelname)s","msg":"%(message)s"}',
    datefmt="%Y-%m-%dT%H:%M:%S",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

BATCH_SIZE = 200
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2


def _load_events_from_jsonl(path: Path) -> list[dict]:
    events = []
    with path.open(encoding="utf-8") as fh:
        for line_num, line in enumerate(fh, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Skipping malformed JSON line",
                    extra={"file": path.name, "line": line_num, "error": str(exc)},
                )
    return events


def _send_batch(client: httpx.Client, api_url: str, events: list[dict]) -> dict:
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            resp = client.post(
                f"{api_url}/events/ingest",
                json={"events": events},
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as exc:
            logger.warning(
                "Ingest HTTP error",
                extra={"attempt": attempt, "status": exc.response.status_code},
            )
        except httpx.RequestError as exc:
            logger.warning(
                "Ingest request failed",
                extra={"attempt": attempt, "error": str(exc)},
            )
        if attempt < RETRY_ATTEMPTS:
            time.sleep(RETRY_DELAY_SECONDS)
    return {"accepted": 0, "rejected": len(events), "duplicates": 0, "errors": []}


def seed_directory(events_dir: Path, api_url: str) -> None:
    jsonl_files = sorted(events_dir.glob("*.jsonl"))
    if not jsonl_files:
        logger.warning("No JSONL files found", extra={"dir": str(events_dir)})
        return

    total_accepted = 0
    total_rejected = 0
    total_duplicates = 0

    with httpx.Client() as client:
        for jsonl_path in jsonl_files:
            events = _load_events_from_jsonl(jsonl_path)
            logger.info(
                "Seeding file",
                extra={"file": jsonl_path.name, "event_count": len(events)},
            )

            for batch_start in range(0, len(events), BATCH_SIZE):
                batch = events[batch_start: batch_start + BATCH_SIZE]
                result = _send_batch(client, api_url, batch)
                total_accepted += result.get("accepted", 0)
                total_rejected += result.get("rejected", 0)
                total_duplicates += result.get("duplicates", 0)

    logger.info(
        "Seed complete",
        extra={
            "accepted": total_accepted,
            "rejected": total_rejected,
            "duplicates": total_duplicates,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed JSONL events into the Store Intelligence API.")
    parser.add_argument("--events-dir", required=True, type=Path)
    parser.add_argument("--api-url", default="http://localhost:8000")
    args = parser.parse_args()

    if not args.events_dir.is_dir():
        logger.error("Events directory not found", extra={"path": str(args.events_dir)})
        sys.exit(1)

    seed_directory(args.events_dir, args.api_url)


if __name__ == "__main__":
    main()
