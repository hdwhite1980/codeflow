#!/usr/bin/env bash
# ============================================================================
# Docker entrypoint
# ----------------------------------------------------------------------------
# Branches on CODEFLOW_ROLE to decide whether this container is the web
# (FastAPI on $PORT) or the worker (build queue consumer).
#
# `set -e` so a failure to launch surfaces immediately as a container exit;
# Railway will redeploy with the previous image. `set -u` catches typos in
# env var names. `set -o pipefail` makes piped failures actually fail.
# ============================================================================
set -euo pipefail

ROLE="${CODEFLOW_ROLE:-web}"
PORT="${PORT:-8000}"

case "$ROLE" in
  web)
    echo "[codeflow] starting web on port $PORT"
    # Uvicorn directly — gunicorn adds no value at single-worker scale and
    # complicates the WebSocket story. Scale horizontally via more Railway
    # replicas, not more workers per instance.
    exec uvicorn app:app \
      --host 0.0.0.0 \
      --port "$PORT" \
      --proxy-headers \
      --forwarded-allow-ips '*'
    ;;
  worker)
    echo "[codeflow] starting worker"
    exec python -m worker
    ;;
  *)
    echo "[codeflow] unknown role: $ROLE" >&2
    exit 1
    ;;
esac
