"""
Correlates POS transaction timestamps to visitor sessions.

A visitor is marked as converted if they were in a billing zone within a 5-minute
window ending at transaction time. Sessions that entered billing but had no POS
follow-up within 10 minutes receive a BILLING_QUEUE_ABANDON event.
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from pipeline.emit import EventEmitter, StoreEvent

logger = logging.getLogger(__name__)

CORRELATION_WINDOW_SECONDS = 300    # 5 min before POS = conversion candidate
ABANDON_WINDOW_SECONDS = 600        # 10 min after billing entry = abandon threshold

BILLING_ZONE_IDS = frozenset({"BILLING", "QUEUE_AREA"})


@dataclass
class POSTransaction:
    order_id: str
    store_id: str
    transaction_ts: datetime
    basket_value_inr: float
    item_count: int
    categories: list[str]


@dataclass
class BillingEntry:
    visitor_id: str
    entry_ts: datetime
    zone_id: str
    session_seq: int
    converted: bool = False


def _parse_transaction_ts(date_str: str, time_str: str) -> datetime:
    # Handles DD-MM-YYYY and HH:MM:SS formats from the Brigade Bangalore export
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            d = datetime.strptime(f"{date_str} {time_str}", f"{fmt} %H:%M:%S")
            return d.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse transaction timestamp: {date_str!r} {time_str!r}")


def load_pos_transactions(pos_csv_path: Path, store_id: str) -> list[POSTransaction]:
    transactions: list[POSTransaction] = []
    with pos_csv_path.open(encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get("store_id") != store_id:
                continue
            try:
                ts = _parse_transaction_ts(row["transaction_date"], row["transaction_time"])
                transactions.append(POSTransaction(
                    order_id=row["order_id"],
                    store_id=row["store_id"],
                    transaction_ts=ts,
                    basket_value_inr=float(row.get("basket_value_inr") or 0),
                    item_count=int(row.get("item_count") or 1),
                    categories=row.get("categories", "").split("|"),
                ))
            except (ValueError, KeyError) as exc:
                logger.warning(
                    "Skipping malformed POS row",
                    extra={"row": dict(row), "error": str(exc)},
                )
    return transactions


class POSCorrelator:
    def __init__(self, transactions: list[POSTransaction]) -> None:
        self._transactions = sorted(transactions, key=lambda t: t.transaction_ts)
        self._billing_entries: list[BillingEntry] = []

    def record_billing_entry(
        self,
        visitor_id: str,
        entry_ts: datetime,
        zone_id: str,
        session_seq: int,
    ) -> None:
        self._billing_entries.append(BillingEntry(
            visitor_id=visitor_id,
            entry_ts=entry_ts,
            zone_id=zone_id,
            session_seq=session_seq,
        ))

    def correlate_and_emit(
        self,
        emitter: EventEmitter,
        store_id: str,
        camera_id: str,
        clip_end_ts: datetime,
    ) -> set[str]:
        """
        Match billing entries to POS transactions.
        Returns set of visitor_ids that were successfully converted.
        Emits BILLING_QUEUE_ABANDON for unmatched billing entries.
        """
        converted_visitor_ids: set[str] = set()

        for entry in self._billing_entries:
            matched = self._find_matching_transaction(entry)
            if matched:
                converted_visitor_ids.add(entry.visitor_id)
                matched_order = matched
                logger.debug(
                    "POS correlation matched",
                    extra={
                        "visitor_id": entry.visitor_id,
                        "order_id": matched_order.order_id,
                        "basket_inr": matched_order.basket_value_inr,
                    },
                )
            else:
                # Only emit ABANDON if there's been enough time since billing entry
                time_since_billing = clip_end_ts - entry.entry_ts
                if time_since_billing.total_seconds() >= ABANDON_WINDOW_SECONDS:
                    emitter.emit(StoreEvent.build(
                        store_id=store_id,
                        camera_id=camera_id,
                        visitor_id=entry.visitor_id,
                        event_type="BILLING_QUEUE_ABANDON",
                        timestamp=entry.entry_ts + timedelta(seconds=ABANDON_WINDOW_SECONDS),
                        confidence=0.9,
                        session_seq=entry.session_seq + 1,
                        zone_id=entry.zone_id,
                        is_staff=False,
                    ))

        return converted_visitor_ids

    def _find_matching_transaction(self, entry: BillingEntry) -> Optional[POSTransaction]:
        window_start = entry.entry_ts
        window_end = entry.entry_ts + timedelta(seconds=ABANDON_WINDOW_SECONDS)

        for txn in self._transactions:
            if window_start <= txn.transaction_ts <= window_end:
                return txn
        return None

    @classmethod
    def from_csv(cls, pos_csv_path: Path, store_id: str) -> "POSCorrelator":
        transactions = load_pos_transactions(pos_csv_path, store_id)
        logger.info(
            "Loaded POS transactions",
            extra={"store_id": store_id, "count": len(transactions), "path": str(pos_csv_path)},
        )
        return cls(transactions)
