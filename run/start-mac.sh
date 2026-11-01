#!/usr/bin/env bash
# Start backend (port from .env BACKEND_PORT, default 8765) + desktop app. Ctrl+C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."
PORT=$(grep -E '^BACKEND_PORT=' .env | cut -d= -f2 | tr -d '[:space:]' || true); PORT=${PORT:-8765}
.venv/bin/python -m backend.server &
BACK=$!
trap 'kill $BACK 2>/dev/null' EXIT
for _ in $(seq 1 30); do curl -sf "http://127.0.0.1:$PORT/health" >/dev/null && break; sleep 0.3; done
cd desktop && BACKEND_PORT=$PORT npm start
