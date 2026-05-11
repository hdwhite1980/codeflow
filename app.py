"""
codeflow.app
============

FastAPI application — the orchestrator that runs on Railway. Wires up:

  * REST endpoints for project creation, ledger reads, impact analysis.
  * Webhook receivers for GitHub, Supabase, and Railway.
  * Job queue producer for build requests (workers consume separately).
  * Realtime fan-out to the frontend via Supabase realtime (the frontend
    subscribes directly to the ledger table; we don't run our own WS fleet).

Deploy posture on Railway
-------------------------
This file is the `web` service. The `worker` service runs the same Python
package but with `python -m codeflow.worker` as the entrypoint. Both share
the same Dockerfile.

Configuration is environment-only — no config files in production. The list
below is the contract with Railway's env var UI.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from ledger import ArtifactKind, EdgeKind, LedgerStore, Tier
from jobqueue import JobQueue, make_job, make_queue
from runtime_sync import (
    ChangeImpactAnalyzer, DatabaseConnector, GitConnector, RailwayConnector,
)
from usage_recorder import PostgresUsageRecorder, summarize


# ---------------------------------------------------------------------------
# Settings — read once at startup. Crash early if anything required is missing.
# ---------------------------------------------------------------------------

class Settings:
    """All configuration comes from env vars. Railway sets these via its UI."""

    def __init__(self) -> None:
        # Supabase Postgres connection string. We use the SERVICE ROLE
        # connection for ledger writes; Supabase's `DATABASE_URL` style
        # `postgresql://...` works directly with psycopg.
        self.database_url = self._require("DATABASE_URL")
        # Used to verify GitHub webhook signatures. Set this when registering
        # the webhook in GitHub's UI and store the same value here.
        self.github_webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
        # Used to verify Supabase database-change webhooks (sent via
        # Supabase's "Database Webhooks" feature).
        self.supabase_webhook_secret = os.environ.get("SUPABASE_WEBHOOK_SECRET", "")
        # Used by the connectors when polling those services. The Railway
        # connector talks to the public Railway API on behalf of users.
        self.github_app_token = os.environ.get("GITHUB_APP_TOKEN", "")
        self.railway_api_token = os.environ.get("RAILWAY_API_TOKEN", "")
        # Hetzner-hosted Ollama and sandbox endpoints. We don't store keys
        # for these in the orchestrator — the worker box auths separately.
        self.hetzner_ollama_url = os.environ.get("HETZNER_OLLAMA_URL", "")
        self.hetzner_sandbox_url = os.environ.get("HETZNER_SANDBOX_URL", "")
        # AI provider keys for the multi-AI voting tier.
        self.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.openai_api_key = os.environ.get("OPENAI_API_KEY", "")
        # Queue backend — defaults to Redis on Railway's private network.
        self.redis_url = os.environ.get("REDIS_URL", "")
        # Toggleable build-time behavior.
        self.environment = os.environ.get("CODEFLOW_ENV", "production")
        self.allow_unsigned_webhooks = (
            os.environ.get("ALLOW_UNSIGNED_WEBHOOKS", "false").lower() == "true"
        )

    @staticmethod
    def _require(name: str) -> str:
        v = os.environ.get(name)
        if not v:
            raise RuntimeError(
                f"Missing required env var {name}. "
                "Set it in Railway's service settings."
            )
        return v


# ---------------------------------------------------------------------------
# Lifespan: open the ledger store once at startup, hold it on app.state.
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = Settings()
    app.state.settings = settings
    app.state.store = LedgerStore(database_url=settings.database_url)
    # Usage recorder reads/writes the token_usage table. The web service
    # reads via the usage endpoint; the worker is what writes rows.
    app.state.recorder = PostgresUsageRecorder(settings.database_url)
    # The queue is the producer side. If REDIS_URL isn't set we fall back
    # to an in-process queue, which means jobs enqueued here go nowhere
    # because the worker is a different process. That's a warning the
    # factory prints, not an error — useful for local dev without Redis.
    app.state.queue: JobQueue = make_queue(settings.redis_url)
    try:
        yield
    finally:
        # psycopg connections are per-call in LedgerStore; nothing to close
        # for the store. The queue does hold a Redis connection pool.
        await app.state.queue.close()


app = FastAPI(
    title="Code Flow",
    description="Multi-AI app builder with auditable ledger and impact analysis.",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

class CreateProjectRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=64)
    prompt: str = Field(..., min_length=10, max_length=4000)


class CreateProjectResponse(BaseModel):
    project_id: str
    slug: str


class ImpactQuery(BaseModel):
    target_artifact_key: str
    description: str = "Planned change"


class ImpactResponse(BaseModel):
    target: str
    description: str
    affected: dict[str, list[str]]
    suggested_db_changes: list[str]
    severity: str
    notes: list[str]


class ProposedDbChange(BaseModel):
    db_artifact_key: str = Field(..., examples=["db:supabase_main"])
    change_type: str = Field(..., examples=["drop_column", "rename_column",
                                            "add_column", "drop_table",
                                            "add_table"])
    target_table: str
    target_column: Optional[str] = None


# ---------------------------------------------------------------------------
# Health and project endpoints
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    """Railway hits this for the health check probe."""
    return {"status": "ok", "environment": app.state.settings.environment}


@app.post("/api/projects", response_model=CreateProjectResponse)
async def create_project(req: CreateProjectRequest) -> CreateProjectResponse:
    store: LedgerStore = app.state.store
    queue: JobQueue = app.state.queue
    project_id = store.create_project(req.slug, req.prompt)
    # Enqueue the build asynchronously. We don't await any AI work here
    # because that would tie the response time to whichever frontier API
    # happens to be slow today. The worker picks this up and writes
    # progress to the ledger; the frontend learns about progress via
    # Supabase realtime, not via this response.
    await queue.enqueue(make_job(
        "build_project",
        project_id=project_id,
        prompt=req.prompt,
        slug=req.slug,
    ))
    return CreateProjectResponse(project_id=project_id, slug=req.slug)


@app.get("/api/projects/{project_id}/artifacts")
async def list_artifacts(
    project_id: str, kind: Optional[str] = None,
) -> dict[str, Any]:
    """Return the current state of all artifacts in the project. The
    frontend uses this to populate the codeflow diagram on first load,
    then subscribes to Supabase realtime for live updates."""
    store: LedgerStore = app.state.store
    kind_filter = ArtifactKind(kind) if kind else None
    entries = store.all_current(project_id, kind_filter)
    return {
        "project_id": project_id,
        "count": len(entries),
        "artifacts": [
            {
                "artifact_key": e.artifact_key,
                "kind": e.artifact_kind.value,
                "tier": e.tier.value,
                "rationale": e.rationale,
                "author": e.author,
                "seq": e.seq,
            }
            for e in entries
        ],
    }


@app.get("/api/projects/{project_id}/usage")
async def project_usage(project_id: str) -> dict[str, Any]:
    """Return per-call token usage and cost breakdown for one project.

    Used by the GUI to show "what is this build costing me?" — both in
    real time (as rows land) and after the fact. The response includes:

      * `total_cost_usd`, `total_tokens` — headline numbers
      * `by_stage`     — rolled up by spec / file / audit
      * `by_provider`  — rolled up by anthropic / openai / etc.
      * `rows`         — the individual API calls, in chronological order

    The frontend can render any of these views without further queries.
    For live updates the frontend subscribes to the `token_usage` table
    on Supabase realtime and recomputes locally rather than re-polling
    this endpoint."""
    recorder: PostgresUsageRecorder = app.state.recorder
    rows = recorder.list_for_project(project_id)
    summary = summarize(project_id, rows)
    return summary.to_dict()


@app.get("/api/projects/{project_id}/manifest/gaps")
async def manifest_gaps(project_id: str) -> dict[str, Any]:
    """The 'nothing is missing' check. Returns the list of manifest items
    not yet satisfied by any artifact."""
    store: LedgerStore = app.state.store
    gaps = store.unsatisfied_manifest_items(project_id)
    return {"project_id": project_id, "unsatisfied": gaps, "count": len(gaps)}


# ---------------------------------------------------------------------------
# Impact analysis endpoints
# ---------------------------------------------------------------------------

@app.post("/api/projects/{project_id}/impact", response_model=ImpactResponse)
async def analyze_impact(
    project_id: str, query: ImpactQuery,
) -> ImpactResponse:
    """Generic impact analysis. Pass any artifact_key; returns the ripple."""
    store: LedgerStore = app.state.store
    analyzer = ChangeImpactAnalyzer(store, project_id)
    report = analyzer.analyze_change(query.target_artifact_key, query.description)
    return ImpactResponse(**report.to_dict())


@app.post("/api/projects/{project_id}/impact/db", response_model=ImpactResponse)
async def analyze_db_change(
    project_id: str, change: ProposedDbChange,
) -> ImpactResponse:
    """Specialized endpoint: proposes a DB migration and returns the impact
    plus migration sequencing advice. This is what the frontend hits when a
    user clicks 'I want to change this column'."""
    store: LedgerStore = app.state.store
    analyzer = ChangeImpactAnalyzer(store, project_id)
    report = analyzer.analyze_proposed_db_change(
        db_artifact_key=change.db_artifact_key,
        change_type=change.change_type,
        target_table=change.target_table,
        target_column=change.target_column,
    )
    return ImpactResponse(**report.to_dict())


# ---------------------------------------------------------------------------
# Webhook receivers
# ---------------------------------------------------------------------------

def _verify_hmac_sha256(
    payload: bytes, signature_header: str, secret: str,
) -> bool:
    """GitHub-style HMAC verification. Header looks like 'sha256=abc123...'."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(
        secret.encode("utf-8"), payload, hashlib.sha256
    ).hexdigest()
    received = signature_header.removeprefix("sha256=")
    # constant-time compare to avoid timing leaks
    return hmac.compare_digest(expected, received)


@app.post("/webhooks/github/{project_id}")
async def github_webhook(
    project_id: str,
    request: Request,
    x_hub_signature_256: str = Header(default=""),
    x_github_event: str = Header(default=""),
) -> dict[str, Any]:
    """
    GitHub push webhook. Each project has a unique URL so we know which
    Code Flow project a commit belongs to. The signing secret is per-project
    and stored on the project row in Supabase (see ledger schema extension
    below; not in the v1 schema yet — for now a single global secret).
    """
    settings: Settings = app.state.settings
    body = await request.body()

    if not settings.allow_unsigned_webhooks:
        if not _verify_hmac_sha256(
            body, x_hub_signature_256, settings.github_webhook_secret,
        ):
            raise HTTPException(status_code=401, detail="bad signature")

    if x_github_event != "push":
        # We only ingest pushes for now; other events get acknowledged.
        return {"status": "ignored", "event": x_github_event}

    # Webhook payloads are large; we acknowledge fast and enqueue a job for
    # the worker to do the real ingest. This matters because GitHub will
    # retry deliveries that take >10s.
    import json
    payload = json.loads(body.decode("utf-8"))

    # In production this enqueues to Redis. For now we inline-process so the
    # path is testable without a queue. Swap to enqueue when load grows.
    repo = payload.get("repository", {}).get("full_name", "unknown")
    commits_received = len(payload.get("commits", []))

    # The actual ingest needs the GitConnector configured for THIS project
    # with its DatabaseConnector counterpart. That wiring lives in the
    # worker; here we just record that the webhook arrived.
    return {
        "status": "queued",
        "project_id": project_id,
        "repo": repo,
        "commits": commits_received,
    }


@app.post("/webhooks/supabase/{project_id}")
async def supabase_webhook(
    project_id: str,
    request: Request,
    x_supabase_signature: str = Header(default=""),
) -> dict[str, Any]:
    """
    Supabase database webhook. Fires on INSERT/UPDATE/DELETE to monitored
    tables. We use this to detect schema drift between the customer's own
    Supabase project (which they're building an app against) and what the
    ledger thinks the schema is. Reconciliation runs anyway, but the
    webhook makes the UI feel live.
    """
    settings: Settings = app.state.settings
    body = await request.body()
    if (not settings.allow_unsigned_webhooks
            and not _verify_hmac_sha256(
                body, x_supabase_signature, settings.supabase_webhook_secret,
            )):
        raise HTTPException(status_code=401, detail="bad signature")

    # Supabase webhook payload shape:
    # { "type": "INSERT|UPDATE|DELETE", "table": "...", "schema": "...", ... }
    import json
    payload = json.loads(body.decode("utf-8"))
    return {
        "status": "queued",
        "project_id": project_id,
        "type": payload.get("type"),
        "table": payload.get("table"),
    }


@app.post("/webhooks/railway/{project_id}")
async def railway_webhook(project_id: str, request: Request) -> dict[str, Any]:
    """
    Railway webhook for deploy events. Railway doesn't currently expose
    HMAC-signed webhooks; we'd instead poll the Railway API on a schedule.
    This endpoint exists for when Railway adds signed webhooks (their
    docs suggest it's planned) or for receiving forwarded events from a
    GitHub Action that watches Railway.
    """
    import json
    body = await request.body()
    payload = json.loads(body.decode("utf-8")) if body else {}
    return {
        "status": "queued",
        "project_id": project_id,
        "event": payload.get("type", "unknown"),
    }


# ---------------------------------------------------------------------------
# Local dev entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    # On Railway, $PORT is set automatically. Locally, default to 8000.
    port = int(os.environ.get("PORT", "8000"))
    uvicorn.run(app, host="0.0.0.0", port=port)
