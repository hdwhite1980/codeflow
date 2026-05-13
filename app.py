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
import json
import os
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, Header, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from event_bus import make_event_bus
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
    # Event bus: subscribed-to by WebSocket connections, published-to by
    # the worker. The web service only reads from it. If REDIS_URL is
    # unset, falls back to an in-memory bus that won't receive worker
    # events (since the worker is a different process) — that's expected
    # in local dev without Redis.
    app.state.event_bus = make_event_bus(settings.redis_url)
    try:
        yield
    finally:
        # psycopg connections are per-call in LedgerStore; nothing to close
        # for the store. The queue does hold a Redis connection pool.
        await app.state.queue.close()
        bus_close = getattr(app.state.event_bus, "aclose", None)
        if bus_close is not None:
            await bus_close()


app = FastAPI(
    title="Code Flow",
    description="Multi-AI app builder with auditable ledger and impact analysis.",
    lifespan=lifespan,
)

# CORS: the frontend lives on a different Railway service / subdomain.
# Allow same-origin and the configured frontend origin. For now permissive
# while we ship the demo; tighten before public launch.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request/response models
# ---------------------------------------------------------------------------

# External service kinds we support in the graph visualization. The
# frontend offers these as a dropdown in the new-build form. "other" is
# the escape hatch for one-offs that don't have a dedicated category yet.
ALLOWED_SERVICE_KINDS = frozenset({
    "postgres", "mysql", "redis",
    "railway", "vercel",
    "github",
    "stripe",
    "anthropic", "openai",
    "other",
})


class ServiceConfig(BaseModel):
    """One external service the user has wired their app to.

    The build pipeline doesn't currently *use* the `config` field — it's
    just metadata the frontend displays in the node's side panel. Later
    work (deploy automation, env-var binding) will read it. For now,
    treat it as opaque user-supplied context.
    """
    kind: str = Field(..., min_length=2, max_length=32)
    label: str = Field(..., min_length=1, max_length=128)
    config: Optional[str] = Field(default=None, max_length=2000)

    @classmethod
    def _validate_kind(cls, v: str) -> str:
        # Pydantic v1-style normalization done in __init__ for compatibility.
        return v


class CreateProjectRequest(BaseModel):
    slug: str = Field(..., min_length=2, max_length=64)
    prompt: str = Field(..., min_length=10, max_length=4000)
    # Services are mandatory: the graph view depends on having at least
    # one external service node to anchor the visualization. Empty arrays
    # are rejected with a clear 422 message.
    services: list[ServiceConfig] = Field(..., min_length=1, max_length=20)


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

    # Validate every service kind is in the allowlist. Pydantic doesn't
    # do this for us because the kind is a plain string — we want a clear
    # 422 with the list of accepted values rather than a 500 later when
    # the ledger write fails on some downstream check.
    for s in req.services:
        if s.kind not in ALLOWED_SERVICE_KINDS:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"Unknown service kind {s.kind!r}. "
                    f"Allowed kinds: {sorted(ALLOWED_SERVICE_KINDS)}."
                ),
            )

    project_id = store.create_project(req.slug, req.prompt)

    # Write each user-supplied external service as a SERVICE artifact in
    # the spec tier. These appear as nodes in the graph visualization
    # and persist across the project's lifecycle. Files generated by
    # the build pipeline can reference them via edges (uses_service,
    # binds_env_var) when import extraction lands in Turn 2.
    for s in req.services:
        # Key shape: service:<kind>:<slugified-label> so multiple services
        # of the same kind can coexist (e.g. two Postgres instances).
        # Slugification is permissive — we don't try to enforce DNS-safe
        # because the key is internal-only.
        key_label = s.label.strip().lower().replace(" ", "-")[:60]
        store.write_entry(
            project_id=project_id,
            tier=Tier.SPEC,
            artifact_kind=ArtifactKind.SERVICE,
            artifact_key=f"service:{s.kind}:{key_label}",
            body={
                "kind": s.kind,
                "label": s.label,
                "config": s.config,
            },
            rationale=(
                f"External service registered at project creation: "
                f"{s.kind} — {s.label}."
            ),
            author="api:create_project",
        )

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


@app.get("/api/projects/{project_id}/audits")
async def project_audits(project_id: str) -> dict[str, Any]:
    """Return the full audit verdict bodies for one project.

    Different from /artifacts (which only returns metadata): this endpoint
    materializes each verdict's blob so the caller sees the actual
    `findings` arrays. Used by:

      * The frontend, to display findings next to each file
      * Operators looking at what an auditor is actually catching
      * Future: the patch loop, to decide which files need re-generation

    Verdicts are keyed by `ref:<project>:audit:<auditor>:<file_path>`.
    We aggregate findings by severity for a quick summary, and include
    raw bodies for clients that want the full detail."""
    store: LedgerStore = app.state.store

    # Pull the current verdict for each (auditor, file) combination. Audit
    # verdicts are stored with artifact_kind=AUDIT_VERDICT, but in the
    # current codebase we use a `ref:` key prefix (which the ledger maps
    # to DECISION_RECORD). Either way, identify by key shape:
    # `ref:<project_id>:audit:<auditor>:<file_path>`. We exclude the
    # bookend decision records (`audit:started|outcome|skipped`).
    BOOKEND_SUFFIXES = (":audit:started", ":audit:outcome", ":audit:skipped")
    all_entries = store.all_current(project_id)
    verdict_entries = [
        e for e in all_entries
        if f":audit:" in e.artifact_key
        and not any(e.artifact_key.endswith(s) for s in BOOKEND_SUFFIXES)
    ]

    # Materialize each verdict's body.
    verdicts: list[dict[str, Any]] = []
    severity_totals = {"critical": 0, "warning": 0, "nit": 0}
    by_auditor: dict[str, dict[str, int]] = {}
    parse_errors = 0

    for e in verdict_entries:
        try:
            blob, _ = store.get_blob(e.blob_sha256)
            body = json.loads(blob.decode("utf-8"))
        except Exception as exc:
            verdicts.append({
                "artifact_key": e.artifact_key,
                "error": f"failed to read verdict body: {exc!r}",
            })
            continue

        findings = body.get("findings", []) if isinstance(body, dict) else []
        auditor = body.get("auditor", "unknown") if isinstance(body, dict) else "unknown"
        file_path = body.get("file_path", "(unknown)") if isinstance(body, dict) else "(unknown)"
        if isinstance(body, dict) and body.get("parse_error"):
            parse_errors += 1

        # Severity tallies.
        per_aud = by_auditor.setdefault(
            auditor, {"critical": 0, "warning": 0, "nit": 0, "files_audited": 0},
        )
        per_aud["files_audited"] += 1
        for f in findings:
            sev = f.get("severity")
            if sev in severity_totals:
                severity_totals[sev] += 1
                per_aud[sev] += 1

        verdicts.append({
            "artifact_key": e.artifact_key,
            "auditor": auditor,
            "file_path": file_path,
            "findings": findings,
            "finding_count": len(findings),
            "parse_error": bool(isinstance(body, dict) and body.get("parse_error")),
            "rationale": e.rationale,
            "seq": e.seq,
        })

    # Stable ordering: by auditor, then by file path. Frontend can re-sort.
    verdicts.sort(key=lambda v: (v.get("auditor", ""), v.get("file_path", "")))

    return {
        "project_id": project_id,
        "verdict_count": len(verdicts),
        "total_findings": sum(severity_totals.values()),
        "by_severity": severity_totals,
        "by_auditor": [
            {"auditor": name, **counts} for name, counts in by_auditor.items()
        ],
        "parse_errors": parse_errors,
        "verdicts": verdicts,
    }


@app.get("/api/projects/{project_id}/graph")
async def project_graph(project_id: str) -> dict[str, Any]:
    """Return nodes + edges shaped for React Flow rendering.

    Response shape
    --------------
    {
      "project_id": "<uuid>",
      "nodes": [
        {
          "id": "<artifact_key>",
          "type": "file" | "external_service" | "spec_entity" | "decision_record",
          "label": "<displayable name>",
          "group": "<folder or category>",
          "status": "pending" | "writing" | "complete" | "failed",
          "data": { ... type-specific payload },
          "seq": <int>
        }
      ],
      "edges": [
        {
          "id": "<from>::<kind>::<to>",
          "source": "<from artifact_key>",
          "target": "<to artifact_key>",
          "kind": "imports" | "uses_service" | "implements" | ...
        }
      ]
    }

    Designed so the frontend can render directly without further
    transformation. Each node carries enough info to draw itself
    (label, group, status badge) and the edges reference nodes by their
    artifact_key (which is the React Flow node id).

    Status semantics
    ----------------
    For files: "complete" once the file artifact exists in the ledger;
    "pending" if we know about it (via spec) but it isn't written yet.
    The "writing" state is set by the live WebSocket layer on the
    frontend, not derived from REST — REST returns "complete" or
    "pending". Failed files (build errors) are inferred from the
    build:outcome decision record's `files_failed` list, but for now
    we just don't expose that since it's rare; will add if needed.
    """
    store: LedgerStore = app.state.store

    # Pull every current artifact. We filter for graph-worthy ones below.
    all_entries = store.all_current(project_id)

    nodes: list[dict[str, Any]] = []
    files_in_ledger: set[str] = set()  # artifact_keys of file entries
    spec_files_seen: set[str] = set()  # paths declared in spec_entity bodies

    for entry in all_entries:
        kind = entry.artifact_kind.value
        # Files: solid green when they exist in the ledger.
        if kind == "file":
            # Key shape: "file:<project_id>:<path>". Strip prefix.
            parts = entry.artifact_key.split(":", 2)
            if len(parts) < 3:
                continue
            path = parts[2]
            files_in_ledger.add(entry.artifact_key)
            nodes.append({
                "id": entry.artifact_key,
                "type": "file",
                "label": path,
                "group": _folder_of(path),
                "status": "complete",
                "data": {
                    "path": path,
                    "rationale": entry.rationale,
                },
                "seq": entry.seq,
            })
        # External services: user-supplied at project creation.
        elif kind == "service":
            try:
                blob, _ = store.get_blob(entry.blob_sha256)
                body = json.loads(blob.decode("utf-8")) if blob else {}
            except Exception:
                body = {}
            nodes.append({
                "id": entry.artifact_key,
                "type": "external_service",
                "label": body.get("label", entry.artifact_key),
                "group": "services",
                "status": "complete",
                "data": {
                    "kind": body.get("kind", "other"),
                    "label": body.get("label", ""),
                    # Don't leak the raw config to the wire by default —
                    # the frontend may show a redacted version. Pass it
                    # through but the frontend should mask secrets.
                    "config": body.get("config"),
                },
                "seq": entry.seq,
            })
        # Spec entities give us "this file is planned" before generation.
        # We surface them as pending file nodes if no file artifact exists yet.
        elif kind == "spec_entity":
            try:
                blob, _ = store.get_blob(entry.blob_sha256)
                body = json.loads(blob.decode("utf-8")) if blob else {}
            except Exception:
                body = {}
            path = body.get("path")
            if path:
                spec_files_seen.add(path)

    # Add "pending" file nodes for spec entries whose files aren't yet
    # in the ledger. Lets the graph reveal the *plan* of the build,
    # not just what's been written so far — important for the "lighting
    # up as files are generated" effect.
    file_paths_in_ledger = {
        n["data"]["path"] for n in nodes if n["type"] == "file"
    }
    for path in spec_files_seen - file_paths_in_ledger:
        # Use the same key shape the build pipeline will use when it
        # actually writes the file, so the frontend can match up the
        # later ledger_entry event with this pending node by id.
        pending_key = f"file:{project_id}:{path}"
        nodes.append({
            "id": pending_key,
            "type": "file",
            "label": path,
            "group": _folder_of(path),
            "status": "pending",
            "data": {"path": path, "rationale": "Planned in spec; not yet generated."},
            "seq": -1,
        })

    # Stable sort: by seq (so freshly-written files appear in build order),
    # then by label as a tiebreaker for pending entries.
    nodes.sort(key=lambda n: (n["seq"], n["label"]))

    # Edges: load all graph_edges for this project and shape them for
    # React Flow. The ledger uses node ids (UUIDs) internally but we
    # want to return artifact_key references so the frontend doesn't
    # have to maintain a separate id-to-key lookup.
    edges = _load_edges(store, project_id)

    return {
        "project_id": project_id,
        "nodes": nodes,
        "edges": edges,
        "node_count": len(nodes),
        "edge_count": len(edges),
    }


def _folder_of(path: str) -> str:
    """Group label for a file. Used as the React Flow node's parent/cluster.
    Top-level files get the group "root"."""
    if "/" not in path:
        return "root"
    return path.rsplit("/", 1)[0]


def _load_edges(store: LedgerStore, project_id: str) -> list[dict[str, Any]]:
    """Return edges as {source, target, kind} dicts using artifact_keys.

    Joins graph_edges → graph_nodes twice (from and to) to translate
    the internal UUIDs back to artifact_keys. We do this in one query
    to avoid N+1 lookups.
    """
    import psycopg
    from psycopg.rows import dict_row
    db_url = app.state.settings.database_url
    edges: list[dict[str, Any]] = []
    sql = """
        SELECT
            e.edge_kind,
            f.artifact_key AS from_key,
            t.artifact_key AS to_key
        FROM graph_edges e
        JOIN graph_nodes f ON f.id = e.from_node_id
        JOIN graph_nodes t ON t.id = e.to_node_id
        WHERE e.project_id = %s
    """
    try:
        with psycopg.connect(db_url, row_factory=dict_row) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (project_id,))
                for row in cur.fetchall():
                    edges.append({
                        "id": f"{row['from_key']}::{row['edge_kind']}::{row['to_key']}",
                        "source": row["from_key"],
                        "target": row["to_key"],
                        "kind": row["edge_kind"],
                    })
    except Exception as exc:
        # Edges are a nice-to-have; without them the frontend can still
        # render disconnected nodes. Don't break the whole endpoint
        # on a transient DB issue.
        print(f"[graph] edge load failed for {project_id}: "
              f"{type(exc).__name__}: {exc}", flush=True)
    return edges


@app.get("/api/projects/{project_id}/manifest/gaps")
async def manifest_gaps(project_id: str) -> dict[str, Any]:
    """The 'nothing is missing' check. Returns the list of manifest items
    not yet satisfied by any artifact."""
    store: LedgerStore = app.state.store
    gaps = store.unsatisfied_manifest_items(project_id)
    return {"project_id": project_id, "unsatisfied": gaps, "count": len(gaps)}


# ---------------------------------------------------------------------------
# Iteration endpoint — apply a follow-up prompt to an existing build.
# ---------------------------------------------------------------------------

class IterateRequest(BaseModel):
    prompt: str = Field(..., min_length=5, max_length=2000)


class IterateResponse(BaseModel):
    project_id: str
    iteration_seq: int
    status: str  # "queued"


@app.post("/api/projects/{project_id}/iterate", response_model=IterateResponse)
async def iterate_project(
    project_id: str, req: IterateRequest,
) -> IterateResponse:
    """Queue an iteration job: take this project as-is and apply the
    iteration prompt as a modification.

    Iteration_seq derivation
    ------------------------
    We assign the next sequence number by counting existing iteration
    decision records. This isn't strictly atomic — two simultaneous
    iterate calls on the same project could collide on the same seq —
    but in practice no user iterates twice in the same millisecond,
    and the second iteration's ledger writes would simply supersede
    the first one's via the standard seq mechanism. We accept this
    rather than introducing a SELECT FOR UPDATE.

    Why we don't block on the build/audit being complete
    -----------------------------------------------------
    A user might submit "actually add a dark mode" while the initial
    build is still running. Their iteration will pick up the latest
    snapshot of the ledger when it actually executes on the worker,
    which is fine: by then either the build will have finished or it
    won't, and the planner will work from whatever state exists. The
    only failure mode is the user iterates against a partial spec
    and gets weird results; their next iteration can correct.
    """
    store: LedgerStore = app.state.store
    queue: JobQueue = app.state.queue

    # Compute next iteration_seq from ledger. We count any decision
    # record whose artifact_key starts with "iteration:" and ends with
    # ":started" to avoid double-counting plan/outcome entries.
    existing = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    started_count = sum(
        1 for e in existing
        if e.artifact_key.startswith("iteration:")
        and e.artifact_key.endswith(":started")
    )
    next_seq = started_count + 1

    await queue.enqueue(make_job(
        "iterate_project",
        project_id=project_id,
        prompt=req.prompt,
        iteration_seq=next_seq,
    ))

    return IterateResponse(
        project_id=project_id,
        iteration_seq=next_seq,
        status="queued",
    )


@app.get("/api/projects/{project_id}/iterations")
async def list_iterations(project_id: str) -> dict[str, Any]:
    """Return all iterations for a project, newest first.

    Each iteration is reconstructed from up to three ledger artifacts:
      - iteration:<N>:started   (always present; written first)
      - iteration:<N>:plan      (present once planning succeeds)
      - iteration:<N>:outcome   (present once iteration finishes — success or failure)

    Status is derived from which artifacts exist:
      - "started" only          → "running"   (rare; only visible mid-iteration)
      - "started" + "plan" only → "running"   (regenerating files)
      - all three               → "complete"  (look at outcome.failed to see if any
                                              files failed; UI can color accordingly)

    We don't have a separate "failed" state at the iteration level today —
    failures inside an iteration show up in the outcome's `failed` list. A
    catastrophic crash (e.g. the Tier.BUILD bug pre-fix) leaves orphan
    started/plan entries with no outcome; we expose those as "running"
    because we can't distinguish them from in-flight iterations from
    ledger state alone. The UI can show "started X minutes ago" so users
    can tell something is stuck.
    """
    store: LedgerStore = app.state.store
    entries = store.all_current(project_id, ArtifactKind.DECISION_RECORD)

    # Group by iteration sequence number.
    by_seq: dict[int, dict[str, Any]] = {}
    for e in entries:
        if not e.artifact_key.startswith("iteration:"):
            continue
        parts = e.artifact_key.split(":")
        if len(parts) != 3:
            continue
        try:
            seq = int(parts[1])
        except ValueError:
            continue
        phase = parts[2]  # "started" | "plan" | "outcome"
        slot = by_seq.setdefault(seq, {})
        try:
            blob, _ = store.get_blob(e.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception:
            body = {}
        slot[phase] = {
            "rationale": e.rationale,
            "body": body,
        }

    iterations = []
    # Build a map: iteration_seq → autopatch_attempt for any iterations
    # that were spawned by the autopatch loop. We discover this by
    # scanning autopatch:N:triggered records and reading their
    # iteration_seq field.
    autopatch_by_iter_seq: dict[int, int] = {}
    for e in entries:
        if not e.artifact_key.startswith("autopatch:") or not e.artifact_key.endswith(":triggered"):
            continue
        try:
            blob, _ = store.get_blob(e.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
            iter_seq = body.get("iteration_seq")
            attempt = body.get("attempt")
            if isinstance(iter_seq, int) and isinstance(attempt, int):
                autopatch_by_iter_seq[iter_seq] = attempt
        except Exception:
            continue

    for seq in sorted(by_seq.keys(), reverse=True):
        slot = by_seq[seq]
        status = "complete" if "outcome" in slot else "running"
        # Derive a one-line summary: prefer the plan rationale (the
        # Builder's own description), fall back to the started prompt,
        # then to a generic message.
        if "plan" in slot:
            summary = slot["plan"]["body"].get("rationale") or "Plan in progress"
        elif "started" in slot:
            summary = slot["started"]["body"].get("prompt", "")[:200]
        else:
            summary = ""
        outcome_body = slot.get("outcome", {}).get("body", {}) if "outcome" in slot else {}
        iterations.append({
            "seq": seq,
            "status": status,
            "prompt": slot.get("started", {}).get("body", {}).get("prompt", ""),
            "started_at": slot.get("started", {}).get("body", {}).get("started_at"),
            "completed_at": outcome_body.get("completed_at"),
            "rationale": summary,
            "changes_applied": outcome_body.get("changes_applied", []),
            "new_files_created": outcome_body.get("new_files_created", []),
            "files_deleted": outcome_body.get("files_deleted", []),
            "failed": outcome_body.get("failed", []),
            "input_tokens": outcome_body.get("input_tokens", 0),
            "output_tokens": outcome_body.get("output_tokens", 0),
            # Non-None if this iteration was triggered by the autopatch
            # loop; carries the attempt number (1, 2, 3...) for display.
            "autopatch_attempt": autopatch_by_iter_seq.get(seq),
        })

    return {
        "project_id": project_id,
        "iterations": iterations,
        "count": len(iterations),
    }


# ---------------------------------------------------------------------------
# Download — bundle the project's current files into a ZIP.
# ---------------------------------------------------------------------------

@app.get("/api/projects/{project_id}/download")
async def download_project(project_id: str):
    """Return all current files for the project as a ZIP archive.

    Includes
    --------
    Only `FILE` artifacts in their current (latest, non-superseded)
    state. Tombstoned files (those whose body starts with the deletion
    marker) are excluded. Audit verdicts, decision records, and spec
    entries are not included — the goal is to give the user a clean
    working tree, not the entire build history.

    Output shape
    ------------
    A standard ZIP with files placed at their original paths
    (`app/main.py`, `requirements.txt`, etc.). Adding a `README.md`
    note about the project's prompt at the top of the zip would be a
    nice touch; not done in this pass.

    Streaming
    ---------
    The zip is constructed entirely in memory because typical projects
    are under 1MB compressed. If we ever generate apps with vendored
    node_modules or large assets, switch to a streaming response with
    a temp file on disk.
    """
    from fastapi.responses import StreamingResponse
    import io
    import zipfile

    store: LedgerStore = app.state.store
    entries = store.all_current(project_id, ArtifactKind.FILE)
    if not entries:
        raise HTTPException(
            status_code=404,
            detail="No files found for this project. Has the build completed?",
        )

    # Pull the slug for a nicer download filename. If the lookup fails
    # we fall back to the project_id.
    slug = project_id[:8]
    try:
        import psycopg
        from psycopg.rows import dict_row
        with psycopg.connect(
            app.state.settings.database_url, row_factory=dict_row,
        ) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT slug FROM projects WHERE id = %s", (project_id,),
                )
                row = cur.fetchone()
                if row:
                    slug = row["slug"]
    except Exception:
        pass  # nice-to-have, not worth failing the download

    # Build the zip in-memory.
    buf = io.BytesIO()
    file_count = 0
    with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
        for entry in entries:
            # Extract path from artifact_key shape "file:<project_id>:<path>"
            parts = entry.artifact_key.split(":", 2)
            if len(parts) < 3:
                continue
            path = parts[2]
            # Skip tombstones — these were "deleted" by iterations.
            if "DELETED in iteration" in entry.rationale:
                continue
            try:
                blob, content_type = store.get_blob(entry.blob_sha256)
            except Exception as exc:
                print(f"[download] failed to load {path}: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                continue
            zf.writestr(path, blob)
            file_count += 1
        # Add a small note at the root so the recipient knows what they
        # have. We don't include the prompt because it could be sensitive.
        zf.writestr(
            "CODEFLOW_README.txt",
            f"Generated by Code Flow.\n"
            f"Project: {slug}\n"
            f"Project ID: {project_id}\n"
            f"Files: {file_count}\n"
            f"This is a starter scaffold. Review before deploying.\n",
        )

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{slug}.zip"',
        },
    )


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


# ---------------------------------------------------------------------------
# Project list endpoint — drives the frontend project picker.
# ---------------------------------------------------------------------------

@app.get("/api/projects")
async def list_projects(limit: int = 50) -> dict[str, Any]:
    """Return recent projects newest-first.

    Used by the frontend's home page to show a list of past builds.
    Returns only metadata (id, slug, prompt, status, created_at); the
    detail view fetches artifacts and findings separately via the
    per-project endpoints.

    Note: this endpoint currently has no auth and returns ALL projects.
    When we add Supabase Auth, it will filter by owner_user_id via RLS."""
    # Bypass the LedgerStore's higher-level API and just hit the table —
    # this is a simple metadata query, no need for the artifact machinery.
    import psycopg
    from psycopg.rows import dict_row
    db_url = app.state.settings.database_url
    limit = max(1, min(limit, 200))  # sanity clamp
    rows: list[dict[str, Any]] = []
    with psycopg.connect(db_url, row_factory=dict_row) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, slug, prompt, status, created_at "
                "FROM projects ORDER BY created_at DESC LIMIT %s",
                (limit,),
            )
            for r in cur.fetchall():
                rows.append({
                    "id": str(r["id"]),
                    "slug": r["slug"],
                    "prompt": r["prompt"],
                    "status": r["status"],
                    "created_at": r["created_at"].isoformat(),
                })
    return {"projects": rows, "count": len(rows)}


# ---------------------------------------------------------------------------
# WebSocket — live updates for one project.
# ---------------------------------------------------------------------------

@app.websocket("/ws/projects/{project_id}")
async def project_events(websocket: WebSocket, project_id: str) -> None:
    """Push live events for one project as they happen.

    Wire protocol
    -------------
    Each frame is a JSON object with at least:
      {"kind": "ledger_entry" | "usage_row", "project_id": "<uuid>", "data": {...}}

    The `data` payload matches the shape of the corresponding REST endpoint's
    row, so the frontend can integrate events into its state without
    special cases — same merge logic for REST fetches and live updates.

    Connection lifecycle
    --------------------
    1. Client opens WS to /ws/projects/<id>.
    2. Server accepts, sends a {"kind": "hello"} frame, then begins
       streaming events as they arrive on the Redis channel.
    3. Either side may close at any time. We unsubscribe and release
       the Redis connection on disconnect.

    Auth note
    ---------
    No auth on the WS yet (matches the REST endpoints). Anyone with the
    project_id can subscribe. When we add Supabase Auth we'll verify
    the JWT during the upgrade handshake and check that the user owns
    the project before subscribing.
    """
    await websocket.accept()
    try:
        await websocket.send_json({
            "kind": "hello",
            "project_id": project_id,
            "message": "Subscribed to live events for this project.",
        })
        bus = app.state.event_bus
        async for event in bus.subscribe(project_id):
            try:
                await websocket.send_json(event)
            except Exception:
                # Send failures usually mean the client disconnected.
                # Break out of the subscribe loop to release Redis resources.
                break
    except WebSocketDisconnect:
        # Normal client-initiated close. Nothing to do.
        pass
    except Exception as exc:
        # Surface unexpected server-side errors in logs but don't crash
        # the whole app. The client will reconnect.
        print(f"[ws] project_events error for {project_id}: "
              f"{type(exc).__name__}: {exc}", flush=True)
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


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
