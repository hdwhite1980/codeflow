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
  * Write at least one ledger entry so there's an audit trail. The
    `received_job` entry is written by the dispatcher before the handler
    runs; handlers add their own outcome entries.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from anthropic_client import AnthropicClient
from build_pipeline import BuildOutcome, run_build
from ledger import ArtifactKind, LedgerStore, Tier


# ---------------------------------------------------------------------------
# Dispatcher context — what every handler gets passed.
# ---------------------------------------------------------------------------

@dataclass
class HandlerContext:
    """Bag of dependencies passed into each handler. Keeps handlers easy
    to test — no module-level state to monkey-patch.

    `anthropic` is Optional because the worker can run without an API key
    (e.g. early in development, or if reconciliation is the only thing
    happening on this replica). Handlers that need it check and refuse
    cleanly instead of crashing the worker on import."""
    store: LedgerStore
    anthropic: Optional[AnthropicClient] = None


# ---------------------------------------------------------------------------
# Handler: build_project
# ---------------------------------------------------------------------------

async def handle_build_project(job: dict[str, Any], ctx: HandlerContext) -> None:
    """Run the build pipeline for one project.

    Writes a decision_record at the start and end so the audit trail
    captures both intent and outcome. The actual work happens in
    build_pipeline.run_build, which writes its own ledger entries for
    every spec item and file it produces."""

    project_id = job.get("project_id")
    prompt = job.get("prompt", "")
    if not project_id:
        print(f"[handlers] build_project: missing project_id in {job!r}, dropping")
        return

    # Hard requirement: an Anthropic client. Without it we can't generate
    # anything. Record a clear ledger entry so the user sees why nothing
    # happened, rather than just timing out.
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

    # Pre-build marker — useful in the frontend for "we started" UX.
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

    # Final outcome record. Always written, success or failure, so the
    # frontend always sees a clear terminal state.
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


# ---------------------------------------------------------------------------
# Registry. Add new handlers here.
# ---------------------------------------------------------------------------

HandlerFn = Callable[[dict[str, Any], HandlerContext], Awaitable[None]]

HANDLERS: dict[str, HandlerFn] = {
    "build_project": handle_build_project,
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
        # We log the full traceback for diagnostics but do not re-raise —
        # see the docstring contract about handlers owning their errors.
        # In a future iteration this is where we'd write a "job_failed"
        # ledger entry and decide whether to re-enqueue.
        print(f"[handlers] kind={kind} raised: {exc!r}")
        print(traceback.format_exc())
