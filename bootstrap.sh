#!/usr/bin/env bash
# bootstrap.sh — one-time dev environment setup
# Usage: bash bootstrap.sh
set -euo pipefail

echo "==> Checking Python version"
python3 --version | grep -qE "3\.(11|12|13)" || {
  echo "ERROR: Python 3.11+ required (found: $(python3 --version))"
  exit 1
}

echo "==> Creating virtual environment"
python3 -m venv .venv
source .venv/bin/activate

echo "==> Installing dependencies"
pip install --upgrade pip
pip install \
  fastapi "uvicorn[standard]" \
  "pydantic>=2.7" \
  aiosqlite \
  httpx \
  python-dotenv \
  rich \
  pytest pytest-cov pytest-asyncio anyio

echo "==> Installing pipeline dependencies (YOLOv8 + OpenCV)"
pip install ultralytics opencv-python-headless numpy

echo "==> Copying env template"
cp .env.example .env

echo "==> Verifying Docker"
docker compose version >/dev/null 2>&1 || { echo "WARNING: docker compose not found — API stack won't start"; }

echo ""
echo "================================================================"
echo " Setup complete."
echo "================================================================"
echo ""
echo "  Start API stack:"
echo "    docker compose up --build"
echo ""
echo "  Run detection pipeline on CCTV clips:"
echo "    CLIPS_DIR=/path/to/clips bash pipeline/run.sh"
echo ""
echo "  Run tests:"
echo "    source .venv/bin/activate"
echo "    pytest --cov=app --cov=pipeline --cov-report=term-missing"
echo ""
echo "  Dashboard:  http://localhost:3000"
echo "  API docs:   http://localhost:8000/docs"
echo "  Health:     http://localhost:8000/health"
echo ""
