"""
FastAPI application entrypoint.

Middleware injects trace_id into request.state and emits structured JSON logs
with endpoint, latency_ms, and status_code on every request.
"""

import asyncio
import json
import logging
import logging.config
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse

from app import anomalies, funnel, health, heatmap, ingestion, metrics
from app.db import close_db, init_db
from app.models import MetricsResponse

_LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
_DB_PATH = os.getenv("DATABASE_URL", "./data/store_intel.db").replace(
    "sqlite+aiosqlite:///", ""
)


def _configure_logging() -> None:
    logging.config.dictConfig({
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "json": {
                "()": "logging.Formatter",
                "fmt": json.dumps({
                    "ts": "%(asctime)s",
                    "level": "%(levelname)s",
                    "logger": "%(name)s",
                    "msg": "%(message)s",
                }),
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
        "root": {"level": _LOG_LEVEL, "handlers": ["stdout"]},
    })


_configure_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    await init_db(_DB_PATH)
    logger.info("API startup complete", extra={"db_path": _DB_PATH})
    yield
    await close_db()
    logger.info("API shutdown complete")


app = FastAPI(
    title="Store Intelligence API",
    version="0.1.0",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _tracing_middleware(request: Request, call_next) -> Response:
    trace_id = uuid.uuid4().hex[:8]
    request.state.trace_id = trace_id

    start = time.perf_counter()
    try:
        response = await call_next(request)
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.info(
            "Request",
            extra={
                "trace_id": trace_id,
                "endpoint": f"{request.method} {request.url.path}",
                "status_code": response.status_code,
                "latency_ms": latency_ms,
            },
        )
        response.headers["X-Trace-Id"] = trace_id
        return response
    except Exception as exc:
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        logger.error(
            "Unhandled exception",
            extra={
                "trace_id": trace_id,
                "endpoint": f"{request.method} {request.url.path}",
                "latency_ms": latency_ms,
                "error": str(exc),
            },
        )
        return JSONResponse(
            status_code=500,
            content={"error": "internal_server_error", "trace_id": trace_id},
        )


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    trace_id = getattr(request.state, "trace_id", "unknown")
    logger.error("Exception handler", extra={"trace_id": trace_id, "error": str(exc)})
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "trace_id": trace_id},
    )


# --- Routers ---
app.include_router(ingestion.router)
app.include_router(metrics.router)
app.include_router(funnel.router)
app.include_router(heatmap.router)
app.include_router(anomalies.router)
app.include_router(health.router)


# --- SSE Streaming endpoint for dashboard ---

async def _compute_metrics_for_store(store_id: str, window: str = "today") -> dict:
    from app.db import get_db
    from app.metrics import get_metrics
    from fastapi import Request as _Req

    class _FakeState:
        trace_id = "sse"

    class _FakeReq:
        state = _FakeState()

    try:
        result = await get_metrics(store_id, _FakeReq(), window=window)  # type: ignore[arg-type]
        return result.model_dump()
    except Exception as exc:
        logger.warning("SSE metrics fetch failed", extra={"store_id": store_id, "error": str(exc)})
        return {"store_id": store_id, "error": str(exc)}


@app.get("/events/stream")
async def event_stream(store_id: str, window: str = "all") -> StreamingResponse:
    async def _generator() -> AsyncIterator[str]:
        while True:
            data = await _compute_metrics_for_store(store_id, window=window)
            yield f"data: {json.dumps(data)}\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(_generator(), media_type="text/event-stream")
