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

import asyncio
import traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from anthropic_client import AnthropicClient
from audit_pipeline import AuditOutcome, run_audit
from build_pipeline import BuildOutcome, run_build
from gemini_client import GeminiClient
from jobqueue import JobQueue, make_job
from ledger import ArtifactKind, LedgerStore, Tier
from openai_client import OpenAIClient
from usage_recorder import UsageRecorder


# ---------------------------------------------------------------------------
# Dispatcher context — what every handler gets passed.
# ---------------------------------------------------------------------------

@dataclass
class HandlerContext:
    """Bag of dependencies passed into each handler. Keeps handlers easy
    to test — no module-level state to monkey-patch.

    All optional fields default to None so tests can construct minimal
    contexts. Handlers that need a specific dependency check it and
    refuse cleanly rather than crashing on missing config.

    The audit step uses BOTH `openai` and `gemini` when both are present
    — same prompt to each, parallel calls, two independent verdicts per
    file. Either being None means that auditor sits out this run; the
    handler still runs the other one."""
    store: LedgerStore
    anthropic: Optional[AnthropicClient] = None
    openai: Optional[OpenAIClient] = None
    gemini: Optional[GeminiClient] = None
    queue: Optional[JobQueue] = None
    recorder: Optional[UsageRecorder] = None


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
        recorder=ctx.recorder,
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
    """Audit the generated files for a project — runs all configured auditors
    in parallel against the same file set.

    Reads file artifacts and spec entries from the ledger, then dispatches
    one `run_audit` call per available auditor (OpenAI + Gemini today).
    Both auditors see the same prompt and the same files; their verdicts
    land under different artifact keys (`ref:...:audit:openai:<path>` vs
    `ref:...:audit:gemini:<path>`), so they don't collide.

    Parallel execution
    ------------------
    We use asyncio.gather so the wall-clock cost of the audit step is
    max(openai, gemini) instead of openai + gemini. Both auditors hit
    independent APIs and write to independent ledger keys, so there is
    no resource contention. The recorder is thread-safe for our purposes
    (it opens fresh DB connections per call), so concurrent writes are fine.

    If neither auditor is configured, write a skip-record."""

    project_id = job.get("project_id")
    if not project_id:
        print(f"[handlers] audit_project: missing project_id in {job!r}, dropping")
        return

    # Collect available auditors. Order doesn't matter — they run concurrently.
    auditors: list[tuple[str, str, Any]] = []  # (auditor_name, provider, client)
    if ctx.openai is not None:
        auditors.append(("openai", "openai", ctx.openai))
    if ctx.gemini is not None:
        auditors.append(("gemini", "google", ctx.gemini))

    if not auditors:
        print(f"[handlers] audit_project: no auditor clients configured; "
              f"writing skip-record for {project_id}")
        ctx.store.write_entry(
            project_id=project_id,
            tier=Tier.AUDIT,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key=f"ref:{project_id}:audit:skipped",
            body={
                "stage": "skipped",
                "reason": "No auditor API keys configured (OPENAI_API_KEY, GEMINI_API_KEY).",
            },
            rationale="Cannot run audit pipeline: no auditor clients available.",
            author="worker:handle_audit_project",
        )
        return

    # Read file artifacts and spec entries from the ledger.
    file_entries = ctx.store.all_current(project_id, ArtifactKind.FILE)
    if not file_entries:
        print(f"[handlers] audit_project: no file artifacts for {project_id}; "
              f"nothing to audit", flush=True)
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
                  f"{fe.artifact_key!r}; skipping", flush=True)
            continue
        path = parts[2]
        try:
            blob, _ = ctx.store.get_blob(fe.blob_sha256)
        except KeyError:
            print(f"[handlers] audit_project: blob missing for {fe.artifact_key}; "
                  f"skipping", flush=True)
            continue
        except Exception as exc:
            print(f"[handlers] audit_project: unexpected error fetching "
                  f"blob for {fe.artifact_key}: {type(exc).__name__}: "
                  f"{exc!r}; skipping", flush=True)
            continue
        spec = spec_by_path.get(path, {})
        file_artifacts.append({
            "path": path,
            "content": blob.decode("utf-8", errors="replace"),
            "purpose": spec.get("purpose", "(unspecified)"),
            "language": spec.get("language", "(unspecified)"),
        })

    # Started marker — note how many auditors are running.
    auditor_names = [name for name, _, _ in auditors]
    ctx.store.write_entry(
        project_id=project_id,
        tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"ref:{project_id}:audit:started",
        body={
            "stage": "started",
            "file_count": len(file_artifacts),
            "auditors": auditor_names,
        },
        rationale=(
            f"Audit pipeline started for {len(file_artifacts)} files with "
            f"auditors: {', '.join(auditor_names)}."
        ),
        author="worker:handle_audit_project",
    )

    # Kick off all auditors concurrently. asyncio.gather preserves order
    # and returns all results in one go. Exceptions inside individual
    # auditors are caught by run_audit itself and surfaced via the
    # AuditOutcome.failed_files list — they won't propagate here.
    audit_tasks = [
        run_audit(
            project_id=project_id,
            file_artifacts=file_artifacts,
            client=client,
            store=ctx.store,
            auditor_name=auditor_name,
            provider=provider,
            recorder=ctx.recorder,
        )
        for auditor_name, provider, client in auditors
    ]
    outcomes: list[AuditOutcome] = await asyncio.gather(*audit_tasks)

    # Aggregate across auditors. Each auditor produced its own set of
    # verdicts and counts; the outcome record summarizes both.
    per_auditor = {}
    total_findings = 0
    total_critical = 0
    total_warning = 0
    total_nit = 0
    total_input_tokens = 0
    total_output_tokens = 0
    for (auditor_name, _, _), outcome in zip(auditors, outcomes):
        per_auditor[auditor_name] = {
            "audited_files": outcome.audited_files,
            "failed_files": [
                {"path": p, "error": err} for p, err in outcome.failed_files
            ],
            "total_findings": outcome.total_findings,
            "critical_count": outcome.critical_count,
            "warning_count": outcome.warning_count,
            "nit_count": outcome.nit_count,
            "input_tokens": outcome.total_input_tokens,
            "output_tokens": outcome.total_output_tokens,
        }
        total_findings += outcome.total_findings
        total_critical += outcome.critical_count
        total_warning += outcome.warning_count
        total_nit += outcome.nit_count
        total_input_tokens += outcome.total_input_tokens
        total_output_tokens += outcome.total_output_tokens

    # Outcome record aggregates across all auditors.
    ctx.store.write_entry(
        project_id=project_id,
        tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"ref:{project_id}:audit:outcome",
        body={
            "auditors": auditor_names,
            "per_auditor": per_auditor,
            "total_findings": total_findings,
            "critical_count": total_critical,
            "warning_count": total_warning,
            "nit_count": total_nit,
            "input_tokens": total_input_tokens,
            "output_tokens": total_output_tokens,
        },
        rationale=(
            f"Audit complete across {len(auditors)} auditors "
            f"({', '.join(auditor_names)}): {total_findings} total findings "
            f"({total_critical} critical, {total_warning} warning, "
            f"{total_nit} nit). "
            f"Tokens: {total_input_tokens} in / {total_output_tokens} out."
        ),
        author="worker:handle_audit_project",
    )
    print(f"[handlers] audit_project: completed for {project_id} — "
          f"{total_findings} findings from {len(auditors)} auditors "
          f"({', '.join(auditor_names)})")


def _json_loads_or_none(blob: bytes):
    """Try to JSON-parse a blob; return None on any failure."""
    import json as _json
    try:
        return _json.loads(blob.decode("utf-8"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# iterate_project — apply a follow-up prompt to an existing project.
# ---------------------------------------------------------------------------

async def handle_iterate_project(job: dict[str, Any], ctx: HandlerContext) -> None:
    """Apply an iteration prompt to an existing project.

    The iteration pipeline:
      1. Plans which files change (Builder makes the call).
      2. Regenerates those files with full cross-context.
      3. Writes new ledger versions; old files supersede.
      4. Enqueues an audit_project job for the SAME project (which will
         audit the latest version of every file — including unchanged
         ones, but that's fine, we get a fresh consistent set of findings
         after each iteration).

    Why we re-audit everything, not just changed files
    --------------------------------------------------
    The user asked for full audit on every iteration. The current
    audit_project handler always audits all current files — we'd need
    a more targeted handler to do subset audits. For now, the simplest
    and most consistent behavior is: re-run the full audit. Each
    iteration shows you a complete, consistent set of findings.
    A future "audit_subset" handler could be cheaper but isn't worth
    the added complexity yet."""

    project_id = job.get("project_id")
    iteration_prompt = job.get("prompt")
    iteration_seq = job.get("iteration_seq")

    if not project_id or not iteration_prompt or iteration_seq is None:
        print(f"[handlers] iterate_project: missing fields in {job!r}; "
              f"dropping", flush=True)
        return

    if ctx.anthropic is None:
        print(f"[handlers] iterate_project: no Anthropic client configured; "
              f"writing skip-record for {project_id}")
        ctx.store.write_entry(
            project_id=project_id,
            tier=Tier.SPEC,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key=f"iteration:{iteration_seq}:skipped",
            body={"reason": "ANTHROPIC_API_KEY not set"},
            rationale="Cannot run iteration without builder client.",
            author="worker:handle_iterate_project",
        )
        return

    # Lazy import to keep build_pipeline as the heavy dep boundary.
    from iterate_pipeline import run_iteration

    print(f"[handlers] iterate_project: starting iteration "
          f"{iteration_seq} for {project_id}", flush=True)

    outcome = await run_iteration(
        project_id=project_id,
        iteration_prompt=iteration_prompt,
        iteration_seq=int(iteration_seq),
        store=ctx.store,
        client=ctx.anthropic,
        recorder=ctx.recorder,
    )

    print(f"[handlers] iterate_project: completed iteration "
          f"{iteration_seq} for {project_id}: "
          f"{len(outcome.changes_applied)} changed, "
          f"{len(outcome.new_files_created)} new, "
          f"{len(outcome.failed)} failed", flush=True)

    # Trigger audit unless this iteration was a no-op (the planner
    # might have said "nothing to do" — no point auditing).
    if (outcome.changes_applied or outcome.new_files_created):
        await ctx.queue.enqueue(make_job(
            "audit_project",
            project_id=project_id,
        ))


# ---------------------------------------------------------------------------
# Registry. Add new handlers here.
# ---------------------------------------------------------------------------

HandlerFn = Callable[[dict[str, Any], HandlerContext], Awaitable[None]]

HANDLERS: dict[str, HandlerFn] = {
    "build_project": handle_build_project,
    "audit_project": handle_audit_project,
    "iterate_project": handle_iterate_project,
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
