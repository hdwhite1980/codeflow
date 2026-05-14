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
import time
import traceback
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from anthropic_client import AnthropicClient
from audit_pipeline import AuditOutcome, run_audit
from build_pipeline import BuildOutcome, run_build
from gemini_client import GeminiClient
from jobqueue import JobQueue, make_job
from ledger import ArtifactKind, LedgerStore, Tier
from ollama_client import OllamaClient
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
    # Local AI client for the guardian. Optional: if absent, guardian
    # job kinds skip rather than crash. The rest of the pipeline works
    # without it.
    ollama: Optional["OllamaClient"] = None


# ---------------------------------------------------------------------------
# Risk client selection (Turn D.1).
# ---------------------------------------------------------------------------

def _make_local_risk_client(ctx: HandlerContext):
    """Construct a risk-analysis LLM client for use inside the worker.

    Tries in order:
      1. ctx.ollama (the worker's local model, when configured) — privacy-
         preserving default for pre/post-iteration risk analysis.
      2. ctx.anthropic — frontier API fallback. Used when the local model
         isn't configured. ANTHROPIC_API_KEY is already in worker env for
         the build/audit pipelines, so this is essentially free to enable.

    Returns None when neither is available — callers must check and skip
    the risk analysis step gracefully.
    """
    if ctx.ollama is not None:
        return ctx.ollama
    if ctx.anthropic is not None:
        return ctx.anthropic
    return None


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

    # Auto-patch loop: if any critical findings exist AND we're under
    # the per-project cap on auto-patch attempts, queue an iteration
    # that asks the Builder to fix them.
    #
    # Why a cap: each auto-patch iteration is essentially a re-build
    # of multiple files plus a full re-audit. Without a cap, a build
    # that keeps surfacing criticals (genuine architectural issue, or
    # the Builder genuinely can't fix it) would loop forever at $0.50+
    # per pass. Three attempts is generous — most fixable criticals
    # resolve on the first patch pass.
    await _maybe_trigger_autopatch(
        project_id=project_id,
        ctx=ctx,
        total_critical=total_critical,
        outcomes=outcomes,
        auditors=auditors,
    )


# Auto-patch tunable. Higher = more chances to fix issues, higher
# cost per build. Three is the sweet spot from manual testing.
AUTOPATCH_MAX_ATTEMPTS = 3


async def _maybe_trigger_autopatch(
    *,
    project_id: str,
    ctx: HandlerContext,
    total_critical: int,
    outcomes: list[AuditOutcome],
    auditors: list[tuple[str, str, Any]],
) -> None:
    """If criticals were found and we're under the attempt cap, queue
    an iteration job to fix them.

    The prompt sent to the Builder is constructed from the actual
    critical-finding bodies read back from the ledger. We deliberately
    don't include warnings or nits — those aren't worth a regeneration
    round (and dumping all findings would blow up the token budget).
    The Builder gets concrete text like:

        "Fix these critical issues found in audit:
         - app/main.py line 21: The component will crash if entry.name is missing
         - app/csv_parser.py line 14: SQL injection via raw f-string interpolation
         ..."

    Why read verdicts back from the ledger
    ---------------------------------------
    AuditOutcome carries only aggregate counts, not the individual
    findings. The full verdict bodies are persisted to the ledger by
    run_audit. We read them back here rather than threading the
    findings list through the AuditOutcome dataclass because the
    autopatch trigger is opt-in: most builds don't need this path,
    and the extra ledger read is cheaper than enlarging the data
    structure that every audit produces.
    """
    if total_critical == 0:
        return

    # Count existing autopatch attempts. We use a decision record kind
    # with artifact_key prefix `autopatch:` so we can find them all.
    decisions = ctx.store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    autopatch_seqs = [
        d for d in decisions
        if d.artifact_key.startswith("autopatch:")
    ]
    next_attempt = len(autopatch_seqs) + 1
    if next_attempt > AUTOPATCH_MAX_ATTEMPTS:
        print(f"[handlers] autopatch: skipping for {project_id} — "
              f"already at {len(autopatch_seqs)}/{AUTOPATCH_MAX_ATTEMPTS} "
              f"attempts. {total_critical} critical findings remain.",
              flush=True)
        ctx.store.write_entry(
            project_id=project_id,
            tier=Tier.AUDIT,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key=f"autopatch:{next_attempt}:capped",
            body={
                "attempt": next_attempt,
                "cap": AUTOPATCH_MAX_ATTEMPTS,
                "critical_remaining": total_critical,
            },
            rationale=(
                f"Autopatch cap reached ({AUTOPATCH_MAX_ATTEMPTS} attempts). "
                f"{total_critical} critical findings remain unaddressed."
            ),
            author="worker:autopatch",
        )
        return

    # Read audit verdicts back from the ledger to extract critical findings.
    # Each verdict's body has shape {"findings": [{"severity": ..., "line": ..., "issue": ..., ...}], "file_path": ...}
    crit_lines = _collect_critical_findings(ctx.store, project_id, max_lines=20)

    if not crit_lines:
        # total_critical > 0 but we couldn't extract any — defensive
        # bail-out rather than send the Builder an empty prompt.
        print(f"[handlers] autopatch: total_critical={total_critical} "
              f"but extracted 0 finding lines. Skipping.", flush=True)
        return

    iteration_prompt = (
        "Fix these critical issues found in the audit. "
        "Make the smallest change that resolves each issue. "
        "Don't rewrite functionality that wasn't flagged.\n\n"
        + "\n".join(crit_lines)
    )

    # Compute the iteration_seq the same way the API does.
    started_count = sum(
        1 for e in decisions
        if e.artifact_key.startswith("iteration:")
        and e.artifact_key.endswith(":started")
    )
    next_iter_seq = started_count + 1

    # Record the autopatch attempt before queueing so we can count it
    # even if the queue write fails. iteration_seq is recorded so the
    # /iterations endpoint can label the resulting iteration as
    # autopatch-originated when the frontend shows the history.
    ctx.store.write_entry(
        project_id=project_id,
        tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"autopatch:{next_attempt}:triggered",
        body={
            "attempt": next_attempt,
            "iteration_seq": next_iter_seq,
            "critical_count": total_critical,
            "critical_lines": crit_lines,
            "iteration_prompt_preview": iteration_prompt[:500],
        },
        rationale=(
            f"Autopatch attempt {next_attempt}/{AUTOPATCH_MAX_ATTEMPTS}: "
            f"queueing iteration {next_iter_seq} to fix "
            f"{len(crit_lines)} critical findings."
        ),
        author="worker:autopatch",
    )

    await ctx.queue.enqueue(make_job(
        "iterate_project",
        project_id=project_id,
        prompt=iteration_prompt,
        iteration_seq=next_iter_seq,
        # Mark this as autopatch-originated so the frontend can show it
        # differently in the iteration history (badge, color, etc).
        autopatch_attempt=next_attempt,
    ))
    print(f"[handlers] autopatch: queued iteration {next_iter_seq} "
          f"to fix {len(crit_lines)} critical findings "
          f"(attempt {next_attempt}/{AUTOPATCH_MAX_ATTEMPTS})",
          flush=True)


def _collect_critical_findings(
    store: LedgerStore, project_id: str, max_lines: int = 20,
) -> list[str]:
    """Read audit_verdict ledger entries and return formatted lines for
    every critical finding, capped at max_lines."""
    import json as _json
    out: list[str] = []
    try:
        verdicts = store.all_current(project_id, ArtifactKind.AUDIT_VERDICT)
    except Exception as exc:
        print(f"[autopatch] failed to load audit verdicts: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return out

    for v in verdicts:
        try:
            blob, _ = store.get_blob(v.blob_sha256)
            body = _json.loads(blob.decode("utf-8")) if blob else {}
        except Exception:
            continue
        file_path = body.get("file_path") or ""
        findings = body.get("findings") or []
        if not isinstance(findings, list):
            continue
        for f in findings:
            if not isinstance(f, dict):
                continue
            if f.get("severity") != "critical":
                continue
            line_info = f" line {f['line']}" if f.get("line") else ""
            issue = (f.get("issue") or "").replace("\n", " ").strip()
            out.append(f"- {file_path}{line_info}: {issue}")
            if len(out) >= max_lines:
                return out
    return out


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

    # Risk integration: pass the local risk client + gate. The iteration
    # writes pre_risk/post_risk records and pauses on critical pre-flight
    # for user confirmation via the API.
    risk_client = _make_local_risk_client(ctx)
    risk_gate = None
    if risk_client is not None:
        try:
            from risk_gate import make_risk_gate
            risk_gate = make_risk_gate()
        except Exception as exc:
            print(f"[handlers] iterate_project: risk gate setup failed: "
                  f"{type(exc).__name__}: {exc}; running without pause",
                  flush=True)

    outcome = await run_iteration(
        project_id=project_id,
        iteration_prompt=iteration_prompt,
        iteration_seq=int(iteration_seq),
        store=ctx.store,
        client=ctx.anthropic,
        recorder=ctx.recorder,
        risk_client=risk_client,
        risk_gate=risk_gate,
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
# fix_all — user-triggered "fix everything" pass.
# ---------------------------------------------------------------------------

async def handle_fix_all(job: dict[str, Any], ctx: HandlerContext) -> None:
    """Run one fix-all pass.

    The pass is structured as:
      1. Read current findings → build prompt → mark `fix_all:N:started`.
      2. Run iteration via the existing iterate pipeline. The iteration's
         own `iteration:K:started/plan/outcome` records track its progress.
      3. Re-audit the project.
      4. Read pre vs post findings, ask Builder to explain remaining
         issues, write `fix_all:N:report`.

    Why not chain via the queue
    ----------------------------
    handle_iterate_project enqueues an audit_project job and returns;
    then handle_audit_project runs separately. If we did the same here,
    the report step (which needs post-audit findings) would have to
    live in handle_audit_project with a "did fix-all trigger me?" check.
    That couples the two handlers awkwardly. Instead, we run the full
    pipeline (iterate + audit + report) inline within this handler. It
    takes longer per job but the control flow is straightforward.

    Note that we call iterate_pipeline.run_iteration and the audit
    pipeline directly, not via the queue. The queue's purpose is to
    decouple HTTP from background work; for an in-handler workflow,
    direct calls are fine and we don't fight asyncio."""

    project_id = job.get("project_id")
    if not project_id:
        print(f"[handlers] fix_all: missing project_id in {job!r}; dropping",
              flush=True)
        return

    if ctx.anthropic is None:
        print(f"[handlers] fix_all: no Anthropic client; skipping",
              flush=True)
        return

    # Lazy imports — keeps the heavy pipeline modules out of this file's
    # top-level import graph.
    from fix_all_pipeline import (
        collect_all_findings, build_fix_all_prompt,
        generate_fix_all_report, write_fix_all_started,
        write_fix_all_report, next_fix_all_seq, FIX_ALL_MAX_FINDINGS,
    )
    from iterate_pipeline import run_iteration

    # Phase 1: collect pre-fix findings.
    pre_findings, truncated = collect_all_findings(
        ctx.store, project_id, max_findings=FIX_ALL_MAX_FINDINGS,
    )
    if not pre_findings:
        # Nothing to do. Still write a marker so the UI can show
        # "fix-all ran, no issues to fix" rather than appearing to hang.
        seq = next_fix_all_seq(ctx.store, project_id)
        write_fix_all_started(ctx.store, project_id, seq, 0, 0)
        write_fix_all_report(
            ctx.store, project_id, seq,
            report="Fix-all pass complete. No issues to fix — the audit was already clean.",
            pre_count=0, post_count=0, fixed=0, regressions=0,
        )
        print(f"[handlers] fix_all: nothing to fix for {project_id}",
              flush=True)
        return

    files_affected = len({f.file_path for f in pre_findings})
    seq = next_fix_all_seq(ctx.store, project_id)
    write_fix_all_started(
        ctx.store, project_id, seq,
        issue_count=len(pre_findings), files_affected=files_affected,
    )

    print(f"[handlers] fix_all: pass {seq} starting for {project_id} — "
          f"{len(pre_findings)} issue(s) across {files_affected} file(s)",
          flush=True)

    # Phase 1.5: pre-flight risk for the fix-all. Same shape as
    # iteration pre-flight: assess the implied changes, pause on
    # critical pending user proceed/cancel.
    if ctx.ollama is not None or os.environ.get("ANTHROPIC_API_KEY"):
        try:
            from guardian_pipeline import (
                analyze_iteration_intent, write_fix_all_risk,
            )
            # Construct a description of what fix-all is about to do.
            fix_prompt_summary = (
                f"Fix-all pass #{seq}: address {len(pre_findings)} audit "
                f"finding(s) across {files_affected} file(s)."
            )
            affected_paths = sorted({f.file_path for f in pre_findings})
            risk_client = _make_local_risk_client(ctx)
            if risk_client is not None:
                pre_risk = await analyze_iteration_intent(
                    store=ctx.store,
                    project_id=project_id,
                    iteration_prompt=fix_prompt_summary,
                    planned_change_paths=affected_paths,
                    planned_delete_paths=[],
                    planned_new_paths=[],
                    client=risk_client,
                )
                write_fix_all_risk(
                    ctx.store, project_id, seq, "pre_risk", pre_risk,
                )
                print(f"[handlers] fix_all: pre_risk for pass {seq}: "
                      f"severity={pre_risk.severity}", flush=True)

                if pre_risk.severity == "critical":
                    from risk_gate import (
                        make_risk_gate, fix_all_gate_key,
                    )
                    gate = make_risk_gate()
                    gate_key = fix_all_gate_key(project_id, seq)
                    print(f"[handlers] fix_all: pass {seq} paused at "
                          f"pre_risk (critical); awaiting decision...",
                          flush=True)
                    decision = await gate.wait_for_decision(gate_key)
                    if decision != "proceed":
                        ctx.store.write_entry(
                            project_id=project_id,
                            tier=Tier.SPEC,
                            artifact_kind=ArtifactKind.DECISION_RECORD,
                            artifact_key=f"fix_all:{seq}:cancelled",
                            body={
                                "fix_all_seq": seq,
                                "reason": decision,
                                "pre_risk_severity": "critical",
                                "cancelled_at": time.time(),
                            },
                            rationale=(
                                f"Fix-all pass {seq} cancelled by user "
                                f"after critical pre-flight risk ({decision})."
                            ),
                            author=f"worker:fix_all:{seq}",
                        )
                        write_fix_all_report(
                            ctx.store, project_id, seq,
                            report=(
                                f"Fix-all pass {seq} cancelled by user "
                                f"after critical pre-flight risk "
                                f"assessment ({decision})."
                            ),
                            pre_count=len(pre_findings),
                            post_count=len(pre_findings),
                            fixed=0, regressions=0,
                        )
                        return
        except Exception as exc:
            print(f"[handlers] fix_all: pre_risk failed: "
                  f"{type(exc).__name__}: {exc}; continuing", flush=True)

    # Phase 2: run a normal iteration. We use the existing iterate
    # pipeline (not a fresh build) because the Builder is good at
    # taking a list-of-issues prompt + current file contents and
    # producing fixed versions.
    iteration_prompt = build_fix_all_prompt(pre_findings, truncated)

    # Compute the iteration_seq the same way the API does.
    decisions = ctx.store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    iteration_seq = sum(
        1 for e in decisions
        if e.artifact_key.startswith("iteration:")
        and e.artifact_key.endswith(":started")
    ) + 1

    # The iteration inherits risk-analysis behavior — it will write
    # its own pre_risk/post_risk records under iteration:<k>:* keys,
    # alongside the fix_all:<seq>:* records this handler writes.
    iteration_risk_client = None
    iteration_risk_gate = None
    try:
        iteration_risk_client = _make_local_risk_client(ctx)
        if iteration_risk_client is not None:
            from risk_gate import make_risk_gate
            iteration_risk_gate = make_risk_gate()
    except Exception:
        pass

    try:
        outcome = await run_iteration(
            project_id=project_id,
            iteration_prompt=iteration_prompt,
            iteration_seq=iteration_seq,
            store=ctx.store,
            client=ctx.anthropic,
            recorder=ctx.recorder,
            risk_client=iteration_risk_client,
            risk_gate=iteration_risk_gate,
        )
    except Exception as exc:
        print(f"[handlers] fix_all: iteration failed: "
              f"{type(exc).__name__}: {exc}", flush=True)
        write_fix_all_report(
            ctx.store, project_id, seq,
            report=f"Fix-all pass {seq} failed during iteration: "
                   f"{type(exc).__name__}: {exc}. "
                   f"The original {len(pre_findings)} issue(s) remain.",
            pre_count=len(pre_findings), post_count=len(pre_findings),
            fixed=0, regressions=0,
        )
        return

    # Phase 3: run the audit inline so we can compare pre vs post.
    # Same logic as handle_audit_project, condensed.
    from audit_pipeline import run_audit
    file_entries = ctx.store.all_current(project_id, ArtifactKind.FILE)
    if file_entries:
        spec_entries = ctx.store.all_current(project_id, ArtifactKind.SPEC_ENTITY)
        spec_by_path: dict[str, dict] = {}
        for s in spec_entries:
            try:
                blob, _ = ctx.store.get_blob(s.blob_sha256)
                data = _json_loads_or_none(blob)
                if isinstance(data, dict) and "path" in data:
                    spec_by_path[data["path"]] = data
            except Exception:
                continue

        file_artifacts = []
        for fe in file_entries:
            parts = fe.artifact_key.split(":", 2)
            if len(parts) < 3:
                continue
            path = parts[2]
            if "DELETED in iteration" in fe.rationale:
                continue
            try:
                blob, _ = ctx.store.get_blob(fe.blob_sha256)
            except Exception:
                continue
            spec = spec_by_path.get(path, {})
            file_artifacts.append({
                "path": path,
                "content": blob.decode("utf-8", errors="replace"),
                "purpose": spec.get("purpose", "(unspecified)"),
                "language": spec.get("language", "(unspecified)"),
            })

        auditors = []
        if ctx.openai is not None:
            auditors.append(("openai", "openai", ctx.openai))
        if ctx.gemini is not None:
            auditors.append(("gemini", "google", ctx.gemini))

        if auditors and file_artifacts:
            audit_tasks = [
                run_audit(
                    project_id=project_id, file_artifacts=file_artifacts,
                    client=client, store=ctx.store,
                    auditor_name=name, provider=provider,
                    recorder=ctx.recorder,
                )
                for name, provider, client in auditors
            ]
            try:
                await asyncio.gather(*audit_tasks)
            except Exception as exc:
                print(f"[handlers] fix_all: post-fix audit failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)

    # Phase 4: compare findings, generate report.
    post_findings, _ = collect_all_findings(
        ctx.store, project_id, max_findings=FIX_ALL_MAX_FINDINGS,
    )
    pre_keys = {(f.file_path, f.severity, f.line or 0, (f.issue or "")[:80])
                for f in pre_findings}
    post_keys = {(f.file_path, f.severity, f.line or 0, (f.issue or "")[:80])
                 for f in post_findings}
    persisted = len(pre_keys & post_keys)
    regressions = len(post_keys - pre_keys)
    fixed = len(pre_findings) - persisted

    # Phase 4.5: post-fix-all risk assessment. Looks at the files that
    # actually changed plus how the findings landscape shifted (regressions
    # are worth assessing more carefully).
    try:
        risk_client_post = _make_local_risk_client(ctx)
        if risk_client_post is not None:
            from guardian_pipeline import (
                analyze_iteration_outcome, write_fix_all_risk,
            )
            post_risk = await analyze_iteration_outcome(
                store=ctx.store,
                project_id=project_id,
                iteration_prompt=(
                    f"Fix-all pass #{seq} just completed. "
                    f"Fixed {fixed} of {len(pre_findings)} issues; "
                    f"{regressions} regression(s)."
                ),
                actual_changed_paths=outcome.changes_applied,
                actual_new_paths=outcome.new_files_created,
                actual_deleted_paths=outcome.files_deleted,
                client=risk_client_post,
            )
            write_fix_all_risk(
                ctx.store, project_id, seq, "post_risk", post_risk,
            )
            print(f"[handlers] fix_all: post_risk for pass {seq}: "
                  f"severity={post_risk.severity}", flush=True)
    except Exception as exc:
        print(f"[handlers] fix_all: post_risk failed: "
              f"{type(exc).__name__}: {exc}", flush=True)

    try:
        report = await generate_fix_all_report(
            project_id=project_id, fix_all_seq=seq,
            pre_findings=pre_findings, post_findings=post_findings,
            client=ctx.anthropic, recorder=ctx.recorder,
            store=ctx.store,
        )
    except Exception as exc:
        print(f"[handlers] fix_all: report generation crashed: "
              f"{type(exc).__name__}: {exc}", flush=True)
        report = (
            f"Fix-all pass {seq} complete (report generation failed). "
            f"Fixed {fixed} of {len(pre_findings)} issue(s); "
            f"{len(post_findings)} remain"
            + (f", {regressions} new" if regressions else "")
            + "."
        )

    write_fix_all_report(
        ctx.store, project_id, seq, report=report,
        pre_count=len(pre_findings), post_count=len(post_findings),
        fixed=fixed, regressions=regressions,
    )
    print(f"[handlers] fix_all: pass {seq} complete for {project_id} — "
          f"fixed {fixed}, remaining {len(post_findings)}, "
          f"regressions {regressions}", flush=True)


# ---------------------------------------------------------------------------
# guardian_index — produce a semantic summary for one file.
# ---------------------------------------------------------------------------

async def handle_guardian_index(job: dict[str, Any], ctx: HandlerContext) -> None:
    """Index one file or one whole project's worth of files.

    Two job shapes:
      - {"project_id": <id>, "file_path": "app/main.py"}  → index one file
      - {"project_id": <id>}                              → index every file
                                                            in the project that
                                                            isn't already indexed
                                                            or whose file artifact
                                                            is newer than its
                                                            semantic summary

    The "index whole project" shape exists for two reasons:
      1. Manually re-running the guardian after the operator turns it on
         for the first time (backfill for existing projects).
      2. Periodic sweeps to catch files whose summaries got missed.

    Skip rules
    ----------
    Tombstones (files marked DELETED in iteration) are skipped — we don't
    want a summary of a deletion marker. Files matching SKIP_PATH_PATTERNS
    in guardian_pipeline are skipped. Files over MAX_INDEXABLE_BYTES are
    skipped. All skips are logged but don't fail the job.
    """
    project_id = job.get("project_id")
    if not project_id:
        print(f"[handlers] guardian_index: missing project_id; dropping",
              flush=True)
        return

    if ctx.ollama is None:
        print(f"[handlers] guardian_index: no Ollama client configured; "
              f"writing skip-record for {project_id}", flush=True)
        ctx.store.write_entry(
            project_id=project_id,
            tier=Tier.SPEC,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key=f"guardian:disabled:{int(time.time())}",
            body={"reason": "OLLAMA_BASE_URL not set or daemon unreachable"},
            rationale="Guardian indexing skipped: no local AI configured.",
            author="worker:handle_guardian_index",
        )
        return

    # Lazy import: keeps the heavy pipeline module out of this file's
    # top-level graph.
    from guardian_pipeline import (
        should_index, summarize_file, write_file_summary,
    )

    target_path = job.get("file_path")

    # Build the work list.
    file_entries = ctx.store.all_current(project_id, ArtifactKind.FILE)
    targets: list[tuple[str, str]] = []  # (path, content)
    for fe in file_entries:
        parts = fe.artifact_key.split(":", 2)
        if len(parts) < 3:
            continue
        path = parts[2]
        if target_path and path != target_path:
            continue
        if "DELETED in iteration" in fe.rationale:
            continue
        try:
            blob, _ = ctx.store.get_blob(fe.blob_sha256)
        except Exception as exc:
            print(f"[guardian] failed to load {path}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            continue
        content = blob.decode("utf-8", errors="replace")
        ok, reason = should_index(path, len(blob))
        if not ok:
            print(f"[guardian] skipping {path}: {reason}", flush=True)
            continue
        targets.append((path, content))

    if not targets:
        print(f"[handlers] guardian_index: no eligible files for "
              f"{project_id}"
              + (f" matching {target_path!r}" if target_path else ""),
              flush=True)
        return

    # Index each file. We do these sequentially because Ollama is single-
    # tenant on the box — parallelism would just serialize at the daemon
    # and add overhead. If we later move to a multi-GPU setup we can
    # parallelize, but for now sequential is simpler and equivalent.
    indexed = 0
    failed: list[tuple[str, str]] = []
    for path, content in targets:
        try:
            language = _language_from_extension(path)
            summary = await summarize_file(
                file_path=path,
                content=content,
                language=language,
                client=ctx.ollama,
            )
            write_file_summary(ctx.store, project_id, summary)
            if ctx.recorder is not None:
                # Record Ollama usage so the cost dashboard shows it,
                # even though dollar cost is zero. Useful for capacity
                # planning and proving the guardian ran.
                ctx.recorder.record(
                    project_id=project_id,
                    provider="ollama",
                    model=summary.indexer_model,
                    stage="guardian:index",
                    subject=path,
                    input_tokens=summary.input_tokens,
                    output_tokens=summary.output_tokens,
                )
            indexed += 1
        except Exception as exc:
            failed.append((path, f"{type(exc).__name__}: {exc}"))
            print(f"[guardian] index failed for {path}: "
                  f"{type(exc).__name__}: {exc}", flush=True)

    print(f"[handlers] guardian_index: completed for {project_id} — "
          f"{indexed} indexed, {len(failed)} failed",
          flush=True)


def _language_from_extension(path: str) -> str:
    """Best-effort language tag for the guardian prompt. The model
    handles unknown languages fine but a hint improves quality."""
    if path.endswith(".py"): return "python"
    if path.endswith((".ts", ".tsx")): return "typescript"
    if path.endswith((".js", ".jsx")): return "javascript"
    if path.endswith(".rs"): return "rust"
    if path.endswith(".go"): return "go"
    if path.endswith(".java"): return "java"
    if path.endswith(".cs"): return "csharp"
    if path.endswith(".rb"): return "ruby"
    if path.endswith(".php"): return "php"
    if path.endswith(".md"): return "markdown"
    if path.endswith((".yml", ".yaml")): return "yaml"
    if path.endswith(".json"): return "json"
    if path.endswith(".sql"): return "sql"
    if path.endswith(".sh"): return "bash"
    if path.endswith(".dockerfile") or path.endswith("Dockerfile"): return "dockerfile"
    return "text"


# ---------------------------------------------------------------------------
# Registry. Add new handlers here.
# ---------------------------------------------------------------------------

HandlerFn = Callable[[dict[str, Any], HandlerContext], Awaitable[None]]

HANDLERS: dict[str, HandlerFn] = {
    "build_project": handle_build_project,
    "audit_project": handle_audit_project,
    "iterate_project": handle_iterate_project,
    "fix_all": handle_fix_all,
    "guardian_index": handle_guardian_index,
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
