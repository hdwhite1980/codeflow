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
from typing import Any, Awaitable, Callable

from ledger import ArtifactKind, LedgerStore, Tier


# ---------------------------------------------------------------------------
# Dispatcher context — what every handler gets passed.
# ---------------------------------------------------------------------------

@dataclass
class HandlerContext:
    """Bag of dependencies passed into each handler. Keeps handlers easy
    to test — no module-level state to monkey-patch."""
    store: LedgerStore


# ---------------------------------------------------------------------------
# Handler: build_project
# ---------------------------------------------------------------------------

async def handle_build_project(job: dict[str, Any], ctx: HandlerContext) -> None:
    """Stub for the multi-AI build pipeline.

    Today this writes one ledger entry saying "we received the job, the
    real pipeline isn't wired yet." That's enough to verify end-to-end
    queue flow and gives us a real audit trail to look at after a test
    POST. The real implementation will replace this body with the spec →
    parallel generation → vote → synthesize → audit loop."""

    project_id = job.get("project_id")
    prompt = job.get("prompt", "")
    if not project_id:
        print(f"[handlers] build_project: missing project_id in {job!r}, dropping")
        return

    # Write a decision_record artifact so the user can see "we got the
    # request, here's what we'd do with it." Once the real pipeline lands,
    # this entry will be the first of many — we keep its key stable so the
    # impact graph treats subsequent runs as supersessions, not new nodes.
    artifact_key = f"plan:{project_id}:initial"
    rationale = (
        f"Received build request for project {project_id}. "
        "The multi-AI orchestration pipeline is not yet wired up; "
        "this entry confirms the queue plumbing works end-to-end. "
        f"Prompt preview: {prompt[:200]}"
    )

    ctx.store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=artifact_key,
        body={
            "stage": "received",
            "prompt_preview": prompt[:200],
            "note": "Multi-AI pipeline not yet wired up; queue plumbing only.",
        },
        rationale=rationale,
        author="worker:handle_build_project",
    )
    print(f"[handlers] build_project: wrote stub plan for {project_id}")


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
