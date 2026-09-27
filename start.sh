#!/usr/bin/env bash
# Starts the FastAPI backend locally for development.
# Production runs on fly.io, see infra/fly/. This script is for editor reload
# and local testing of /demo, /healthz, etc.
#
# Usage:
#   ./start.sh                 # runs on $BACKEND_PORT (default 8080)
#   BACKEND_PORT=9000 ./start.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$SCRIPT_DIR/.tmp/logs"
mkdir -p "$LOG_DIR"

UVICORN_LOG="$LOG_DIR/uvicorn.log"
BACKEND_PORT="${BACKEND_PORT:-8080}"

green()  { printf '\033[0;32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[0;33m%s\033[0m\n' "$*"; }
red()    { printf '\033[0;31m%s\033[0m\n' "$*"; }
bold()   { printf '\033[1m%s\033[0m\n' "$*"; }

UVICORN_PID=""
cleanup() {
  echo ""
  yellow "Shutting down…"
  [ -n "$UVICORN_PID" ] && kill "$UVICORN_PID" 2>/dev/null && wait "$UVICORN_PID" 2>/dev/null || true
  green "Stopped."
}
trap cleanup EXIT INT TERM

if [ ! -f "$SCRIPT_DIR/.env" ]; then
  red "ERROR: .env not found at $SCRIPT_DIR/.env"
  exit 1
fi

PYTHON="$SCRIPT_DIR/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  red "ERROR: venv not found at $SCRIPT_DIR/.venv"
  exit 1
fi

if fuser "$BACKEND_PORT/tcp" &>/dev/null; then
  yellow "Port $BACKEND_PORT in use, stopping existing process…"
  fuser -k "$BACKEND_PORT/tcp" 2>/dev/null || true
  sleep 1
fi

bold "Starting FastAPI on port $BACKEND_PORT…"
cd "$SCRIPT_DIR"
"$PYTHON" -m uvicorn tools.llm_proxy.main:app \
  --host 0.0.0.0 \
  --port "$BACKEND_PORT" \
  --reload \
  > "$UVICORN_LOG" 2>&1 &
UVICORN_PID=$!

for i in $(seq 1 15); do
  if curl -sf "http://localhost:$BACKEND_PORT/healthz" &>/dev/null; then
    green "Backend ready at http://localhost:$BACKEND_PORT"
    break
  fi
  if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
    red "Backend crashed. Tail of $UVICORN_LOG:"
    tail -n 50 "$UVICORN_LOG"
    exit 1
  fi
  sleep 1
done

bold "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
green "Local dev:    http://localhost:$BACKEND_PORT"
green "Health:       http://localhost:$BACKEND_PORT/healthz"
green "Backend log:  $UVICORN_LOG"
yellow "For voice (Twilio Media Streams) testing you need a public host, "
yellow "either deploy to fly.io (fly deploy --config infra/fly/fly.toml)"
yellow "or run a tunnel (cloudflared / ngrok) and point Twilio at it."
bold "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
yellow "Press Ctrl-C to stop."
echo ""

tail -f "$UVICORN_LOG" &
wait "$UVICORN_PID"
