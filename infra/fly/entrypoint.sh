#!/usr/bin/env bash
set -euo pipefail

PORT="${BACKEND_PORT:-8080}"
APP_USER="app"

# Run the given command as the unprivileged app user (S9). When the container
# starts as root (the default), first chown the Fly volume, it is created
# root-owned and the app user can't write /data/transcripts.db otherwise, then
# drop privileges with gosu (exec, so signals/PID 1 behave). If we're already
# non-root (e.g. local dev), just exec.
run_as_app() {
  if [[ "$(id -u)" == "0" ]]; then
    if [[ -d /data ]]; then
      chown -R "${APP_USER}:${APP_USER}" /data || true
    fi
    exec gosu "${APP_USER}" "$@"
  fi
  exec "$@"
}

# GOOGLE_SERVICE_ACCOUNT_JSON is provided as a fly secret (the JSON string).
# Materialize it to a file (as root, before dropping) so sheets_writer (which
# reads from GOOGLE_SERVICE_ACCOUNT_FILE) keeps working unchanged.
if [[ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ]]; then
  printf '%s' "${GOOGLE_SERVICE_ACCOUNT_JSON}" > /app/google-service-account.json
  chown "${APP_USER}:${APP_USER}" /app/google-service-account.json 2>/dev/null || true
  export GOOGLE_SERVICE_ACCOUNT_FILE=/app/google-service-account.json
fi

case "${1:-app}" in
  app)
    run_as_app uvicorn tools.llm_proxy.main:app \
      --host 0.0.0.0 \
      --port "${PORT}" \
      --proxy-headers \
      --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}"
    ;;
  retention)
    export RETENTION_ALLOW_APPLY=1
    run_as_app python -m tools.run_daily_retention
    ;;
  *)
    echo "entrypoint.sh: unknown process: $1" >&2
    exit 1
    ;;
esac
