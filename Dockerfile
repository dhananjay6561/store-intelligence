# Hugging Face Spaces (Docker SDK) image.
# Serves the FastAPI API + dashboard, and seeds the bundled 5,499 events on startup.
FROM python:3.11-slim

# HF Spaces requires the container to run as a non-root user (uid 1000).
RUN useradd -m -u 1000 user

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir \
        fastapi "uvicorn[standard]" pydantic aiosqlite python-dotenv httpx

# Application source + bundled data (events, store layout, POS export).
COPY --chown=user app/       ./app/
COPY --chown=user data/      ./data/
COPY --chown=user dashboard/ ./dashboard/
COPY --chown=user scripts/   ./scripts/
COPY --chown=user start.sh   ./start.sh

RUN chmod +x ./start.sh && mkdir -p ./data/events && chown -R user:user /app

USER user

ENV DATABASE_URL="sqlite+aiosqlite:///./data/store_intel.db" \
    STORE_LAYOUT_PATH="./data/store_layout.json" \
    LOG_LEVEL="INFO" \
    PORT="7860" \
    STALE_FEED_MINUTES="999999" \
    DEAD_ZONE_MINUTES="999999"

EXPOSE 7860

CMD ["./start.sh"]
