# Multi-stage build — builder installs deps, runtime image is lean
FROM python:3.11-slim AS builder

WORKDIR /build
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        fastapi \
        "uvicorn[standard]" \
        pydantic \
        aiosqlite \
        python-dotenv

FROM python:3.11-slim AS runtime

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Application source
COPY app/      ./app/
COPY data/     ./data/
COPY pipeline/ ./pipeline/

RUN mkdir -p /app/data/events

ENV DATABASE_URL="sqlite+aiosqlite:///./data/store_intel.db" \
    STORE_LAYOUT_PATH="./data/store_layout.json" \
    LOG_LEVEL="INFO" \
    API_HOST="0.0.0.0" \
    API_PORT="8000"

EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health')"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
