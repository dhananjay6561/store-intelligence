"""
Async SQLite connection pool using aiosqlite with WAL journal mode.
Schema migrations run automatically on startup via _apply_migrations().
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

import aiosqlite

logger = logging.getLogger(__name__)

_DDL_EVENTS = """
CREATE TABLE IF NOT EXISTS events (
    event_id        TEXT PRIMARY KEY,
    store_id        TEXT NOT NULL,
    camera_id       TEXT NOT NULL,
    visitor_id      TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    timestamp       TEXT NOT NULL,
    zone_id         TEXT,
    dwell_ms        INTEGER NOT NULL DEFAULT 0,
    is_staff        INTEGER NOT NULL DEFAULT 0,
    confidence      REAL NOT NULL,
    queue_depth     INTEGER,
    sku_zone        TEXT,
    session_seq     INTEGER,
    raw_json        TEXT NOT NULL,
    ingested_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);
"""

_DDL_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_events_store_ts ON events(store_id, timestamp);
CREATE INDEX IF NOT EXISTS idx_events_visitor   ON events(visitor_id);
CREATE INDEX IF NOT EXISTS idx_events_type      ON events(event_type);
CREATE INDEX IF NOT EXISTS idx_events_store_type ON events(store_id, event_type);
"""

_DDL_SESSIONS_VIEW = """
CREATE VIEW IF NOT EXISTS sessions AS
SELECT
    store_id,
    visitor_id,
    MIN(timestamp)  AS session_start,
    MAX(timestamp)  AS session_end,
    COUNT(*)        AS event_count,
    MAX(CASE WHEN event_type = 'EXIT' THEN 1 ELSE 0 END)              AS has_exit,
    MAX(CASE WHEN event_type = 'BILLING_QUEUE_JOIN' THEN 1 ELSE 0 END) AS reached_billing,
    MAX(CASE WHEN event_type = 'BILLING_QUEUE_ABANDON' THEN 1 ELSE 0 END) AS abandoned_billing
FROM events
WHERE is_staff = 0
GROUP BY store_id, visitor_id;
"""

_PRAGMA_WAL = "PRAGMA journal_mode=WAL;"
_PRAGMA_FK  = "PRAGMA foreign_keys=ON;"


class Database:
    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._conn: Optional[aiosqlite.Connection] = None
        # Serialize all DB access — aiosqlite's single connection is not safe for concurrent callers
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._conn = await aiosqlite.connect(self._db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute(_PRAGMA_WAL)
        await self._conn.execute(_PRAGMA_FK)
        await self._apply_migrations()
        logger.info("Database connected", extra={"path": self._db_path})

    async def disconnect(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None
            logger.info("Database disconnected")

    async def _apply_migrations(self) -> None:
        assert self._conn is not None
        await self._conn.execute(_DDL_EVENTS)
        for stmt in _DDL_INDEXES.strip().split("\n"):
            stmt = stmt.strip()
            if stmt:
                await self._conn.execute(stmt)
        await self._conn.execute(_DDL_SESSIONS_VIEW)
        await self._conn.commit()

    async def ping(self) -> bool:
        try:
            assert self._conn is not None
            await self._conn.execute("SELECT 1")
            return True
        except Exception:
            return False

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        assert self._conn is not None
        try:
            yield self._conn
            await self._conn.commit()
        except Exception:
            await self._conn.rollback()
            raise

    async def execute(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        assert self._conn is not None
        async with self._lock:
            return await self._conn.execute(sql, params)

    async def executemany(self, sql: str, params_seq: list[tuple]) -> None:
        assert self._conn is not None
        async with self._lock:
            await self._conn.executemany(sql, params_seq)
            await self._conn.commit()

    async def fetchone(self, sql: str, params: tuple = ()) -> Optional[aiosqlite.Row]:
        assert self._conn is not None
        async with self._lock:
            cursor = await self._conn.execute(sql, params)
            return await cursor.fetchone()

    async def fetchall(self, sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
        assert self._conn is not None
        async with self._lock:
            cursor = await self._conn.execute(sql, params)
            return await cursor.fetchall()


_db: Optional[Database] = None


def get_db() -> Database:
    assert _db is not None, "Database not initialised — call init_db() first"
    return _db


async def init_db(db_path: str) -> Database:
    global _db
    _db = Database(db_path)
    await _db.connect()
    return _db


async def close_db() -> None:
    global _db
    if _db:
        await _db.disconnect()
        _db = None
