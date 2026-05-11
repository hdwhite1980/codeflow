# syntax=docker/dockerfile:1.7
# ============================================================================
# Code Flow Dockerfile
# ----------------------------------------------------------------------------
# One image, two entrypoints. Railway runs this image twice — once as the
# `web` service (FastAPI on $PORT) and once as the `worker` service (build
# queue consumer). The runtime decides which by inspecting CODEFLOW_ROLE.
#
# We use python:3.12-slim because:
#   * psycopg[binary] ships prebuilt wheels for it (no compile-time deps)
#   * httpx + fastapi work without modification
#   * the image stays under 200 MB after pruning, which keeps Railway
#     deploys fast.
#
# We do NOT bundle Ollama or any model weights here. Those live on Hetzner.
# This image's only job is to be the orchestrator/worker.
# ============================================================================

FROM python:3.12-slim AS base

# Standard hygiene: no .pyc files, no buffered stdout, no pip cache.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Build deps for psycopg if we ever lose the binary wheel fallback.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. Railway doesn't require it, but it's good hygiene and
# protects against subtle filesystem mishaps in any generated code that
# might accidentally execute in this container (it shouldn't — that's what
# the Hetzner sandbox is for — but defense in depth).
RUN useradd --create-home --shell /bin/bash codeflow
WORKDIR /home/codeflow/app

# ----------------------------------------------------------------------------
# Dependency layer (cached aggressively)
# ----------------------------------------------------------------------------
COPY requirements.txt .
RUN pip install -r requirements.txt

# ----------------------------------------------------------------------------
# Application layer
# ----------------------------------------------------------------------------
# Copy the whole package after deps so a code-only change doesn't bust the
# pip layer.
COPY ledger.py runtime_sync.py symbol_extractor.py kinds_v2.py \
     app.py hetzner_client.py worker.py \
     ./

# Ownership and permissions.
RUN chown -R codeflow:codeflow /home/codeflow/app
USER codeflow

# Railway sets PORT at runtime; we bind to it. CODEFLOW_ROLE picks between
# web and worker. Default is web because that's the more common case and
# fails loud and obvious if misconfigured.
ENV CODEFLOW_ROLE=web

# The startup script branches on CODEFLOW_ROLE. We use exec form so signals
# propagate cleanly (Railway sends SIGTERM on redeploy and expects clean
# shutdown within 30s).
COPY --chown=codeflow:codeflow docker-entrypoint.sh .
RUN chmod +x docker-entrypoint.sh

# Health check uses curl against /health. Railway has its own HTTP probe,
# but the Docker HEALTHCHECK is useful when running this image locally.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl --fail --silent http://localhost:${PORT:-8000}/health || exit 1

ENTRYPOINT ["./docker-entrypoint.sh"]
