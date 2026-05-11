"""
codeflow.job_handlers
=====================

Handlers for the job kinds the worker can process. Kept separate from
worker.py so the worker loop only has to know "given a job, dispatch it"
and the actual work lives in named functions per kind.

Adding a new kind
-----------------
1. Write an async handler function with signature
   `async def handle_<kind>(job: dict, ctx: HandlerContext) -> None`.
2. Register it in HANDLERS at the bottom of the file.
3. Producers (in app.py or worker reconciliation) can now emit jobs with
   that kind and the worker will dispatch automatically.

Handler responsibilities
------------------------
Each handler is the unit of work. It must:

  * Be idempotent (or fail loudly) — at-least-once semantics are easier
    than exactly-once, and we may re-enqueue jobs in retry paths.
  * Catch its own exceptions and log them. An unhandled exception that
    escapes will bubble up to the worker loop, which will log it but keep
    running. Either way, the job is gone — we don't reliably retry yet.
  * Write at least one ledger entry so there's an audit trail.

Job chaining
------------
Handlers can enqueue follow-on jobs by accepting a queue in their
context. `handle_build_project` does this: after the build succeeds, it
enqueues an `audit_project` job so the audit step runs asynchronously
on the next worker loop iteration. The user sees build outcome
immediately; audit outcome lands shortly after.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from anthropic_client import AnthropicClient
from audit_pipeline import AuditOutcome, run_audit
from build_pipeline import BuildOutcome, run_build
from jobqueue import JobQueue, make_job
from ledger import ArtifactKind, LedgerStore, Tier
from openai_client import OpenAIClient


# ---------------------------------------------------------------------------
# Dispatcher context — what every handler gets passed.
# ---------------------------------------------------------------------------

@dataclass
class HandlerContext:
    """Bag of dependencies passed into each handler. Keeps handlers easy
    to test — no module-level state to monkey-patch.

    All optional fields default to None so tests can construct minimal
    contexts. Handlers that need a specific dependency check it and
    refuse cleanly rather than crashing on missing config."""
    store: LedgerStore
    anthropic: Optional[AnthropicClient] = None
    openai: Optional[OpenAIClient] = None
    queue: Optional[JobQueue] = None


# ---------------------------------------------------------------------------
# Handler: build_project
# ---------------------------------------------------------------------------

async def handle_build_project(job: dict[str, Any], ctx: HandlerContext) -> None:
    """Run the build pipeline for one project.

    Writes a decision_record at the start and end so the audit trail
    captures both intent and outcome. The actual work happens in
    build_pipeline.run_build, which writes its own ledger entries for
    every spec item and file it produces.

    On success, enqueues an `audit_project` job so the audit step runs
    independently. We don't audit inline because audit may take a few
    seconds per file and we'd rather surface the generated files to the
    user immediately."""

    project_id = job.get("project_id")
    prompt = job.get("prompt", "")
    if not project_id:
        print(f"[handlers] build_project: missing project_id in {job!r}, dropping")
        return

    # Hard requirement: an Anthropic client.
    if ctx.anthropic is None:
        print(f"[handlers] build_project: ANTHROPIC_API_KEY not configured; "
              f"writing skip-record for {project_id}")
        ctx.store.write_entry(
            project_id=project_id,
            tier=Tier.SPEC,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key=f"ref:{project_id}:build:skipped",
            body={
                "stage": "skipped",
                "reason": "ANTHROPIC_API_KEY not configured on worker",
                "prompt_preview": prompt[:200],
            },
            rationale="Cannot run build pipeline: no Anthropic client available.",
            author="worker:handle_build_project",
        )
        return

    # Pre-build marker.
    ctx.store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"ref:{project_id}:build:started",
        body={"stage": "started", "prompt_preview": prompt[:200]},
        rationale=f"Build pipeline started for project {project_id}.",
        author="worker:handle_build_project",
    )

    outcome: BuildOutcome = await run_build(
        project_id=project_id,
        prompt=prompt,
        client=ctx.anthropic,
        store=ctx.store,
    )

    # Final outcome record.
    ctx.store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"ref:{project_id}:build:outcome",
        body={
            "succeeded": outcome.succeeded,
            "spec_summary": outcome.spec.summary if outcome.spec else None,
            "files_written": outcome.files_written,
            "files_failed": [{"path": p, "error": err}
                             for p, err in outcome.files_failed],
            "input_tokens": outcome.total_input_tokens,
            "output_tokens": outcome.total_output_tokens,
        },
        rationale=(
            f"Build {'succeeded' if outcome.succeeded else 'completed with failures'}: "
            f"{len(outcome.files_written)} files written, "
            f"{len(outcome.files_failed)} failed. "
            f"Tokens used: {outcome.total_input_tokens} in / "
            f"{outcome.total_output_tokens} out."
        ),
        author="worker:handle_build_project",
    )
    print(f"[handlers] build_project: completed for {project_id} — "
          f"{len(outcome.files_written)} written, {len(outcome.files_failed)} failed")

    # Chain into audit if we wrote any files.
    if outcome.files_written and ctx.queue is not None:
        await ctx.queue.enqueue(make_job(
            "audit_project",
            project_id=project_id,
        ))
        print(f"[handlers] build_project: enqueued audit_project for {project_id}")


# ---------------------------------------------------------------------------
# Handler: audit_project
# ---------------------------------------------------------------------------

async def handle_audit_project(job: dict[str, Any], ctx: HandlerContext) -> None:
    """Audit the generated files for a project.

    Reads all current file artifacts from the ledger, fetches the matching
    spec entries for purpose/language context, then asks the OpenAI auditor
    to review each file. Writes one audit_verdict per file."""

    project_id = job.get("project_id")
    if not project_id:
        print(f"[handlers] audit_project: missing project_id in {job!r}, dropping")
        return

    if ctx.openai is None:
        print(f"[handlers] audit_project: OPENAI_API_KEY not configured; "
              f"writing skip-record for {project_id}")
        ctx.store.write_entry(
            project_id=project_id,
            tier=Tier.AUDIT,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key=f"ref:{project_id}:audit:skipped",
            body={
                "stage": "skipped",
                "reason": "OPENAI_API_KEY not configured on worker",
            },
            rationale="Cannot run audit pipeline: no OpenAI client available.",
            author="worker:handle_audit_project",
        )
        return

    # Read file artifacts and spec entries from the ledger.
    file_entries = ctx.store.all_current(project_id, ArtifactKind.FILE)
    if not file_entries:
        print(f"[handlers] audit_project: no file artifacts for {project_id}; "
              f"nothing to audit")
        return

    spec_entries = ctx.store.all_current(project_id, ArtifactKind.SPEC_ENTITY)
    # Build a lookup from path → spec entry body, so we can pass purpose
    # and language to the auditor for context.
    spec_by_path: dict[str, dict] = {}
    for s in spec_entries:
        try:
            blob, _ = ctx.store.get_blob(s.blob_sha256)
            data = _json_loads_or_none(blob)
            if isinstance(data, dict) and "path" in data:
                spec_by_path[data["path"]] = data
        except Exception:
            # Don't let a stale spec entry break the whole audit run.
            continue

    # Assemble file artifact summaries for the audit pipeline.
    file_artifacts = []
    for fe in file_entries:
        # Strip "file:<uuid>:" prefix to get the path. Use split with
        # maxsplit=2 so paths containing colons aren't mangled.
        parts = fe.artifact_key.split(":", 2)
        if len(parts) < 3:
            print(f"[handlers] audit_project: malformed file key "
                  f"{fe.artifact_key!r}; skipping")
            continue
        path = parts[2]
        try:
            blob, _ = ctx.store.get_blob(fe.blob_sha256)
        except KeyError:
            print(f"[handlers] audit_project: blob missing for {fe.artifact_key}; "
                  f"skipping")
            continue
        spec = spec_by_path.get(path, {})
        file_artifacts.append({
            "path": path,
            "content": blob.decode("utf-8", errors="replace"),
            "purpose": spec.get("purpose", "(unspecified)"),
            "language": spec.get("language", "(unspecified)"),
        })

    # Started marker.
    ctx.store.write_entry(
        project_id=project_id,
        tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"ref:{project_id}:audit:started",
        body={"stage": "started", "file_count": len(file_artifacts)},
        rationale=f"Audit pipeline started for {len(file_artifacts)} files.",
        author="worker:handle_audit_project",
    )

    outcome: AuditOutcome = await run_audit(
        project_id=project_id,
        file_artifacts=file_artifacts,
        client=ctx.openai,
        store=ctx.store,
        auditor_name="openai",
    )

    # Outcome record.
    ctx.store.write_entry(
        project_id=project_id,
        tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"ref:{project_id}:audit:outcome",
        body={
            "all_clean": outcome.all_clean,
            "audited_files": outcome.audited_files,
            "failed_files": [{"path": p, "error": err}
                             for p, err in outcome.failed_files],
            "total_findings": outcome.total_findings,
            "critical_count": outcome.critical_count,
            "warning_count": outcome.warning_count,
            "nit_count": outcome.nit_count,
            "input_tokens": outcome.total_input_tokens,
            "output_tokens": outcome.total_output_tokens,
        },
        rationale=(
            f"Audit complete: {len(outcome.audited_files)} files audited, "
            f"{outcome.total_findings} findings "
            f"({outcome.critical_count} critical, "
            f"{outcome.warning_count} warning, "
            f"{outcome.nit_count} nit). "
            f"Tokens: {outcome.total_input_tokens} in / "
            f"{outcome.total_output_tokens} out."
        ),
        author="worker:handle_audit_project",
    )
    print(f"[handlers] audit_project: completed for {project_id} — "
          f"{outcome.total_findings} findings across "
          f"{len(outcome.audited_files)} files")


def _json_loads_or_none(blob: bytes):
    """Try to JSON-parse a blob; return None on any failure."""
    import json as _json
    try:
        return _json.loads(blob.decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Registry. Add new handlers here.
# ---------------------------------------------------------------------------

HandlerFn = Callable[[dict[str, Any], HandlerContext], Awaitable[None]]

HANDLERS: dict[str, HandlerFn] = {
    "build_project": handle_build_project,
    "audit_project": handle_audit_project,
}


async def dispatch(job: dict[str, Any], ctx: HandlerContext) -> None:
    """Look up the handler for this job's kind and run it. Unknown kinds
    are logged and dropped — we never crash the worker on a typo."""
    kind = job.get("kind")
    if not kind:
        print(f"[handlers] dropping job with no kind: {job!r}")
        return
    handler = HANDLERS.get(kind)
    if handler is None:
        print(f"[handlers] no handler for kind={kind!r}; dropping job")
        return
    try:
        await handler(job, ctx)
    except Exception as exc:
        print(f"[handlers] kind={kind} raised: {exc!r}")
        print(traceback.format_exc())
