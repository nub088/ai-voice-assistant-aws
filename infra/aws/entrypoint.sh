#!/usr/bin/env sh
set -eu
exec uvicorn tools.llm_proxy.main:app --host 0.0.0.0 --port "${BACKEND_PORT:-8080}"
