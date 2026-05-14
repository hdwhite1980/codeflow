"""
codeflow.iterate_pipeline
=========================

Iterative builds. Given an existing project and a user prompt like
"add a dark mode toggle", we:

  1. Ask the Builder to plan which files change (full file list +
     iteration prompt → JSON of changes/new/delete).
  2. For each file marked for change: fetch current content from the
     ledger, send it back to the Builder with the iteration prompt,
     get new content, write a new ledger version (supersedes via seq).
  3. For new files: run normal _generate_file flow.
  4. For deletions: write a tombstone ledger entry with deleted=True
     in the body so the graph can hide the node.
  5. Audit changed + new files (skipping ones that didn't change).

Design choices
--------------
* **Builder picks scope.** We could have the user multi-select files
  before generation, but in a chat-style iteration UX, "what to change"
  is part of the prompt's meaning. Letting the Builder propose and
  execute in one step keeps the loop tight. If the proposal is wrong,
  the user iterates again.

* **Full content as context.** Each regenerated file's prompt includes
  the full current contents of files it might depend on (specifically:
  the files the Builder marked as "change" plus a structural summary
  of the rest). This protects against drift — the new file imports
  match what the other files actually export.

* **Iteration as ledger artifact.** We write an ITERATION entry at
  the start (status=running) and update it at the end (status=complete
  or failed). The graph view can show "Iteration 3: add dark mode"
  in a sidebar or history pane in a future turn.

* **Cost tracking.** All usage rows use stage="iteration:<N>" where N
  is the iteration sequence number. This lets the frontend show per-
  iteration cost without changing the underlying schema.

Failure semantics
-----------------
If the plan step fails, we write a failed iteration entry and stop —
no changes touch the ledger. If individual file regenerations fail,
we keep going (just like build_pipeline does for fresh builds) and
record the failures in the iteration outcome.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from anthropic_client import AnthropicClient, AnthropicError
from build_pipeline import (
    ProjectSpec,
    SpecFile,
    _generate_file,
    _load_service_refs,
    file_artifact_key,
)
from ledger import ArtifactKind, EdgeKind, LedgerStore, Tier
from usage_recorder import UsageRecorder


_PROVIDER = "anthropic"
_PLAN_MODEL_MAX_TOKENS = 4096
_MAX_FILES_PER_ITERATION = 30  # safety cap; one iteration shouldn't touch the whole repo


# ---------------------------------------------------------------------------
# Plan-phase data shapes.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IterationChange:
    path: str
    reason: str


@dataclass(frozen=True)
class IterationPlan:
    """What the Builder decided to do in response to the iteration prompt.

    changes: existing files to regenerate (path must already exist in ledger)
    new_files: paths to create from scratch (path must NOT already exist)
    deletes: existing files to tombstone (path must already exist)
    rationale: Builder's free-text explanation of the plan, for the
        iteration log and the user-facing iteration history
    """
    changes: list[IterationChange]
    new_files: list[SpecFile]  # reuse SpecFile so _generate_file works
    deletes: list[str]
    rationale: str

    def is_empty(self) -> bool:
        return (
            not self.changes and not self.new_files and not self.deletes
        )


@dataclass(frozen=True)
class IterationOutcome:
    iteration_seq: int
    changes_applied: list[str]
    new_files_created: list[str]
    files_deleted: list[str]
    failed: list[tuple[str, str]]  # (path, reason)
    input_tokens: int
    output_tokens: int
    rationale: str


# ---------------------------------------------------------------------------
# Prompts.
# ---------------------------------------------------------------------------

PLAN_SYSTEM_PROMPT = """You are a senior engineer planning a precise, minimal change to an existing codebase.

You will be shown:
  1. The current file inventory (paths + 1-line purposes).
  2. A user request describing what should change.

Your job is to decide exactly which files need to change, which need to be created, and which (if any) should be deleted. Make the SMALLEST change that satisfies the request. Touch as few files as possible.

Output STRICT JSON (no markdown fences, no commentary). Schema:
{
  "rationale": "<1-3 sentences explaining the plan>",
  "changes": [
    {"path": "<existing file path>", "reason": "<why this file changes>"}
  ],
  "new_files": [
    {"path": "<new file path>", "purpose": "<1-line description>", "language": "<python|typescript|markdown|...>", "estimated_lines": <int>}
  ],
  "delete": ["<path>"]
}

Rules:
  - Paths in `changes` and `delete` MUST exist in the inventory.
  - Paths in `new_files` MUST NOT exist in the inventory.
  - Prefer changing existing files over creating new ones.
  - If the request is unclear or impossible given the inventory, return all-empty lists and explain in rationale.
  - Maximum 30 entries across all three lists combined.
"""


REGEN_SYSTEM_PROMPT = """You are editing an existing source file in a codebase. The user has requested a change. Your job is to produce the COMPLETE new contents of the file — not a diff, not a patch, the whole file.

You will be shown:
  - The file's current contents
  - The user's iteration request
  - A list of OTHER files in the project (for context)
  - Optionally, the FULL contents of files this file depends on

Rules:
  - Output ONLY the file's new contents. No markdown fences, no commentary, no explanation.
  - Preserve existing functionality unless the iteration request explicitly contradicts it.
  - Keep the same language and conventions as the original.
  - If the request can't be applied to this file (it's the wrong file for the change), return the original contents unchanged.
"""


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------

async def run_iteration(
    *,
    project_id: str,
    iteration_prompt: str,
    iteration_seq: int,
    store: LedgerStore,
    client: AnthropicClient,
    recorder: Optional[UsageRecorder] = None,
    risk_client: Optional[Any] = None,
    risk_gate: Optional[Any] = None,
) -> IterationOutcome:
    """Top-level entry point. The job_handler calls this.

    Returns IterationOutcome. Writes ledger entries for:
      * iteration:<seq>:started   (at top)
      * iteration:<seq>:plan      (the plan JSON, after planning phase)
      * iteration:<seq>:pre_risk  (optional — when risk_client provided)
      * iteration:<seq>:cancelled (only if user cancelled at pre_risk)
      * file:<project_id>:<path>  (one per changed/new file, supersedes)
      * iteration:<seq>:post_risk (optional — when risk_client provided)
      * iteration:<seq>:outcome   (at bottom — status, costs, errors)

    Audit dispatch is the job_handler's responsibility, not ours.
    We just return the list of paths that changed so it can audit them.

    Risk integration (Turn D.1)
    ---------------------------
    When `risk_client` is provided (any LLM client with .complete()),
    pre-flight and post-iteration risk assessments run automatically.
    Pre-flight runs between planning and regeneration. If the
    assessment severity is `critical`, the iteration pauses on
    `risk_gate.wait_for_decision()` until the user proceeds or
    cancels via API.

    When `risk_client` is None, risk analysis is skipped entirely —
    the pre-guardian iteration flow runs unchanged.
    """
    stage_tag = f"iteration:{iteration_seq}"

    # Record iteration start so the graph view can show "iterating…"
    # even if planning takes 30+ seconds.
    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"iteration:{iteration_seq}:started",
        body={
            "iteration_seq": iteration_seq,
            "prompt": iteration_prompt,
            "started_at": time.time(),
        },
        rationale=f"Iteration {iteration_seq} started.",
        author=f"worker:iterate:{iteration_seq}",
    )

    # Wrap the entire rest in a try/except so that no matter what blows
    # up — a typo, a network glitch, a schema mismatch — we write an
    # :outcome record with the error before returning. Without this,
    # an unexpected exception leaves the iteration appearing to be
    # "still running" forever from the UI's perspective. The outer
    # dispatcher logs the traceback to worker logs but the user sees
    # only :started in the artifacts list — same shape as a hang.
    try:
        return await _run_iteration_body(
            project_id=project_id,
            iteration_prompt=iteration_prompt,
            iteration_seq=iteration_seq,
            stage_tag=stage_tag,
            store=store,
            client=client,
            recorder=recorder,
            risk_client=risk_client,
            risk_gate=risk_gate,
        )
    except Exception as exc:
        print(f"[iterate] UNEXPECTED FAILURE in iteration {iteration_seq} "
              f"for {project_id}: {type(exc).__name__}: {exc}", flush=True)
        import traceback
        traceback.print_exc()
        outcome = IterationOutcome(
            iteration_seq=iteration_seq,
            changes_applied=[], new_files_created=[], files_deleted=[],
            failed=[("(internal)", f"{type(exc).__name__}: {str(exc)[:300]}")],
            input_tokens=0, output_tokens=0,
            rationale=f"Iteration failed unexpectedly: {type(exc).__name__}",
        )
        _write_outcome(store, project_id, iteration_seq, outcome)
        return outcome


async def _run_iteration_body(
    *,
    project_id: str,
    iteration_prompt: str,
    iteration_seq: int,
    stage_tag: str,
    store: LedgerStore,
    client: AnthropicClient,
    recorder: Optional[UsageRecorder] = None,
    risk_client: Optional[Any] = None,
    risk_gate: Optional[Any] = None,
) -> IterationOutcome:
    """The actual iteration logic, extracted so run_iteration can wrap
    it in a single try/except that always writes an outcome record.

    Common failure modes the inner try/except catches:
      - Anthropic API auth/rate limit during the plan phase
      - Model returns invalid JSON (already handled by _parse_plan)
      - Network blip during the regen phase
    Per-file failures (in the regen loop) are caught inside the loop
    and recorded in the failed list — they don't abort the iteration.
    """
    try:
        inventory = _current_inventory(store, project_id)
        plan, plan_in, plan_out = await _plan_iteration(
            client=client, project_id=project_id, stage_tag=stage_tag,
            inventory=inventory, iteration_prompt=iteration_prompt,
            recorder=recorder,
        )
    except Exception as exc:
        print(f"[iterate] plan phase failed for {project_id} "
              f"iter {iteration_seq}: {type(exc).__name__}: {exc}",
              flush=True)
        outcome = IterationOutcome(
            iteration_seq=iteration_seq,
            changes_applied=[], new_files_created=[], files_deleted=[],
            failed=[("(plan)", f"{type(exc).__name__}: {str(exc)[:200]}")],
            input_tokens=0, output_tokens=0,
            rationale=f"Planning failed: {type(exc).__name__}",
        )
        _write_outcome(store, project_id, iteration_seq, outcome)
        return outcome

    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"iteration:{iteration_seq}:plan",
        body={
            "iteration_seq": iteration_seq,
            "rationale": plan.rationale,
            "changes": [c.path for c in plan.changes],
            "new_files": [f.path for f in plan.new_files],
            "deletes": list(plan.deletes),
        },
        rationale=f"Plan for iteration {iteration_seq}: {plan.rationale[:200]}",
        author=f"worker:iterate:{iteration_seq}",
    )

    # Pre-flight risk analysis. Runs when risk_client is configured.
    # Pause-on-critical behavior: if the assessment is `critical`, we
    # block on risk_gate.wait_for_decision() until the user proceeds or
    # cancels via API. Other severities log the assessment for the
    # frontend to surface but don't interrupt the iteration.
    #
    # Any failure in the risk step is swallowed and logged — we never
    # block iteration progress on a risk analysis error, only on a
    # real critical user-confirmation requirement.
    if risk_client is not None and not plan.is_empty():
        try:
            from guardian_pipeline import (
                analyze_iteration_intent, write_iteration_risk,
            )
            pre_risk = await analyze_iteration_intent(
                store=store,
                project_id=project_id,
                iteration_prompt=iteration_prompt,
                planned_change_paths=[c.path for c in plan.changes],
                planned_delete_paths=list(plan.deletes),
                planned_new_paths=[f.path for f in plan.new_files],
                client=risk_client,
            )
            write_iteration_risk(
                store, project_id, iteration_seq, "pre_risk", pre_risk,
            )
            print(f"[iterate] pre_risk for iter {iteration_seq}: "
                  f"severity={pre_risk.severity}, "
                  f"confidence={pre_risk.confidence:.2f}", flush=True)

            # Pause on critical, wait for proceed/cancel signal.
            if pre_risk.severity == "critical" and risk_gate is not None:
                from risk_gate import iteration_gate_key
                gate_key = iteration_gate_key(project_id, iteration_seq)
                print(f"[iterate] iter {iteration_seq} paused at pre_risk "
                      f"(critical); awaiting user decision...", flush=True)
                decision = await risk_gate.wait_for_decision(gate_key)
                print(f"[iterate] iter {iteration_seq} decision: {decision}",
                      flush=True)
                if decision != "proceed":
                    # User cancelled or timed out. Write a cancellation
                    # record and bail out with a clean outcome — no
                    # regeneration, no audit, no post_risk.
                    store.write_entry(
                        project_id=project_id,
                        tier=Tier.SPEC,
                        artifact_kind=ArtifactKind.DECISION_RECORD,
                        artifact_key=f"iteration:{iteration_seq}:cancelled",
                        body={
                            "iteration_seq": iteration_seq,
                            "reason": decision,
                            "pre_risk_severity": pre_risk.severity,
                            "cancelled_at": time.time(),
                        },
                        rationale=(
                            f"Iteration {iteration_seq} cancelled by user "
                            f"after critical pre-flight risk ({decision})."
                        ),
                        author=f"worker:iterate:{iteration_seq}",
                    )
                    outcome = IterationOutcome(
                        iteration_seq=iteration_seq,
                        changes_applied=[], new_files_created=[],
                        files_deleted=[], failed=[],
                        input_tokens=plan_in, output_tokens=plan_out,
                        rationale=(
                            f"Cancelled by user after critical pre-flight "
                            f"risk assessment ({decision})."
                        ),
                    )
                    _write_outcome(store, project_id, iteration_seq, outcome)
                    return outcome
        except Exception as exc:
            print(f"[iterate] pre_risk analysis failed for iter "
                  f"{iteration_seq}: {type(exc).__name__}: {exc}",
                  flush=True)
            # Continue without pre-flight assessment. The iteration
            # still runs; only the risk panel will be empty.

    if plan.is_empty():
        outcome = IterationOutcome(
            iteration_seq=iteration_seq,
            changes_applied=[], new_files_created=[], files_deleted=[],
            failed=[], input_tokens=plan_in, output_tokens=plan_out,
            rationale=plan.rationale,
        )
        _write_outcome(store, project_id, iteration_seq, outcome)
        return outcome

    # Phase 2: load existing file contents we'll need.
    # We send full content of all changed-files as cross-context to each
    # regeneration. For projects up to ~50K total tokens this is fine;
    # beyond that we'd need a smarter summarization layer.
    file_blobs = _load_file_blobs(store, project_id, inventory)
    change_paths = {c.path for c in plan.changes}

    # Spec-like wrapper so _generate_file works for new files.
    pseudo_spec = ProjectSpec(
        summary=f"Iteration {iteration_seq}: {plan.rationale}",
        files=list(plan.new_files) + [
            SpecFile(
                path=c.path,
                purpose=inventory.get(c.path, "(existing file)"),
                language=_language_of(c.path),
                imports=[],
                size_hint="medium",
            )
            for c in plan.changes
        ],
    )

    changes_applied: list[str] = []
    new_files_created: list[str] = []
    failed: list[tuple[str, str]] = []
    total_in = plan_in
    total_out = plan_out

    # Pre-compute guardian context once for the whole iteration. The
    # Builder will see paths + purposes + risk notes for every file in
    # the project, not just bare paths. Empty string if no summaries
    # exist yet — falls back to inventory-only context downstream.
    # We exclude files currently being regenerated; their summary would
    # be stale by the time the Builder writes the new version.
    from guardian_pipeline import build_pipeline_context
    being_changed = {c.path for c in plan.changes}
    guardian_context = build_pipeline_context(
        store, project_id,
        focus_paths=sorted(being_changed),
        exclude_paths=sorted(being_changed),
    )

    # Phase 3a: regenerate changed files. Each gets the iteration prompt
    # baked into its system message plus the current file content as the
    # initial assistant turn.
    for change in plan.changes:
        try:
            new_content, tin, tout = await _regenerate_file(
                client=client, project_id=project_id, stage_tag=stage_tag,
                target_path=change.path, change_reason=change.reason,
                iteration_prompt=iteration_prompt,
                current_content=file_blobs.get(change.path, ""),
                inventory=inventory,
                related_blobs=file_blobs,
                related_paths=change_paths - {change.path},
                guardian_context=guardian_context,
                recorder=recorder,
            )
            total_in += tin
            total_out += tout
            store.write_entry(
                project_id=project_id,
                tier=Tier.GENERATION,
                artifact_kind=ArtifactKind.FILE,
                artifact_key=file_artifact_key(project_id, change.path),
                body=new_content,
                rationale=(
                    f"Regenerated in iteration {iteration_seq}: "
                    f"{change.reason}"
                ),
                author=f"worker:iterate:{iteration_seq}",
            )
            changes_applied.append(change.path)
        except AnthropicError as exc:
            failed.append((change.path, f"AnthropicError: {exc.body[:200]}"))
            if exc.status_code in (401, 403):
                break
        except Exception as exc:
            failed.append((change.path, f"{type(exc).__name__}: {str(exc)[:200]}"))

    # Phase 3b: new files via the existing _generate_file flow.
    for new_file in plan.new_files:
        try:
            content, tin, tout = await _generate_file(
                spec=pseudo_spec, target=new_file, client=client,
                project_id=project_id, recorder=recorder,
                guardian_context=guardian_context,
            )
            total_in += tin
            total_out += tout
            store.write_entry(
                project_id=project_id,
                tier=Tier.GENERATION,
                artifact_kind=ArtifactKind.FILE,
                artifact_key=file_artifact_key(project_id, new_file.path),
                body=content,
                rationale=(
                    f"Created in iteration {iteration_seq} "
                    f"to satisfy: {iteration_prompt[:100]}"
                ),
                author=f"worker:iterate:{iteration_seq}",
            )
            new_files_created.append(new_file.path)
        except AnthropicError as exc:
            failed.append((new_file.path, f"AnthropicError: {exc.body[:200]}"))
            if exc.status_code in (401, 403):
                break
        except Exception as exc:
            failed.append((new_file.path, f"{type(exc).__name__}: {str(exc)[:200]}"))

    # Phase 3c: deletions are tombstones. We write a file entry with a
    # special body and rationale; the graph view can filter these out.
    files_deleted: list[str] = []
    for path in plan.deletes:
        try:
            store.write_entry(
                project_id=project_id,
                tier=Tier.GENERATION,
                artifact_kind=ArtifactKind.FILE,
                artifact_key=file_artifact_key(project_id, path),
                body=f"# DELETED in iteration {iteration_seq}\n",
                rationale=(
                    f"Deleted in iteration {iteration_seq}: "
                    f"superseded or no longer needed."
                ),
                author=f"worker:iterate:{iteration_seq}",
            )
            files_deleted.append(path)
        except Exception as exc:
            failed.append((path, f"delete failed: {type(exc).__name__}"))

    outcome = IterationOutcome(
        iteration_seq=iteration_seq,
        changes_applied=changes_applied,
        new_files_created=new_files_created,
        files_deleted=files_deleted,
        failed=failed,
        input_tokens=total_in,
        output_tokens=total_out,
        rationale=plan.rationale,
    )

    # Post-iteration risk analysis. Runs against the files that
    # ACTUALLY changed, which may differ from the planner's prediction.
    # Same skip-on-failure semantics as pre-flight: if risk_client is
    # absent or the analysis crashes, we log and continue. The iteration
    # outcome is unaffected.
    if risk_client is not None and (changes_applied or new_files_created or files_deleted):
        try:
            from guardian_pipeline import (
                analyze_iteration_outcome, write_iteration_risk,
            )
            post_risk = await analyze_iteration_outcome(
                store=store,
                project_id=project_id,
                iteration_prompt=iteration_prompt,
                actual_changed_paths=changes_applied,
                actual_new_paths=new_files_created,
                actual_deleted_paths=files_deleted,
                client=risk_client,
            )
            write_iteration_risk(
                store, project_id, iteration_seq, "post_risk", post_risk,
            )
            print(f"[iterate] post_risk for iter {iteration_seq}: "
                  f"severity={post_risk.severity}, "
                  f"confidence={post_risk.confidence:.2f}", flush=True)
        except Exception as exc:
            print(f"[iterate] post_risk analysis failed for iter "
                  f"{iteration_seq}: {type(exc).__name__}: {exc}",
                  flush=True)

    _write_outcome(store, project_id, iteration_seq, outcome)
    return outcome


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _current_inventory(
    store: LedgerStore, project_id: str,
) -> dict[str, str]:
    """Return {file_path: 1-line summary} for the current state of the
    project. Uses each file's rationale field as the summary because
    that's typically a one-liner from the original spec."""
    entries = store.all_current(project_id, ArtifactKind.FILE)
    out: dict[str, str] = {}
    for e in entries:
        # Skip tombstones.
        if "DELETED in iteration" in e.rationale:
            continue
        parts = e.artifact_key.split(":", 2)
        if len(parts) < 3:
            continue
        out[parts[2]] = e.rationale[:200]
    return out


def _load_file_blobs(
    store: LedgerStore, project_id: str, inventory: dict[str, str],
) -> dict[str, str]:
    """Load full text content for each existing file. Used as context
    in regeneration. Skips tombstones automatically (they aren't in
    inventory)."""
    out: dict[str, str] = {}
    entries = store.all_current(project_id, ArtifactKind.FILE)
    for e in entries:
        parts = e.artifact_key.split(":", 2)
        if len(parts) < 3:
            continue
        path = parts[2]
        if path not in inventory:
            continue
        try:
            blob, _ = store.get_blob(e.blob_sha256)
            out[path] = blob.decode("utf-8", errors="replace")
        except Exception:
            continue
    return out


async def _plan_iteration(
    *,
    client: AnthropicClient,
    project_id: str,
    stage_tag: str,
    inventory: dict[str, str],
    iteration_prompt: str,
    recorder: Optional[UsageRecorder],
) -> tuple[IterationPlan, int, int]:
    """Send the Builder the inventory + prompt; parse the JSON plan it
    returns. Validates that paths in changes/delete exist and new_files
    don't. Invalid entries are dropped (logged but not raised) so we
    can still try to do *something* rather than reject the whole plan."""
    inventory_lines = "\n".join(
        f"  - {p}: {s}" for p, s in sorted(inventory.items())
    ) or "  (no files yet — this is the first iteration)"
    user_prompt = (
        f"Current file inventory ({len(inventory)} files):\n"
        f"{inventory_lines}\n\n"
        f"User request: {iteration_prompt}\n\n"
        "Respond with the JSON plan."
    )

    result = await client.complete(
        user_prompt,
        system=PLAN_SYSTEM_PROMPT,
        max_tokens=_PLAN_MODEL_MAX_TOKENS,
    )
    if recorder is not None:
        recorder.record(
            project_id=project_id,
            provider=_PROVIDER,
            model=result.model,
            stage=f"{stage_tag}:plan",
            subject=None,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    plan = _parse_plan(result.text, inventory)
    return plan, result.input_tokens, result.output_tokens


def _parse_plan(raw_text: str, inventory: dict[str, str]) -> IterationPlan:
    """Strict JSON parsing + filtering. Anything that doesn't match
    the schema gets dropped with a logged warning; we don't raise so
    a partial plan can still execute."""
    text = raw_text.strip()
    # Strip code fences if the model added them despite instructions.
    if text.startswith("```"):
        # Drop the first line (```json) and last line (```).
        lines = text.splitlines()
        text = "\n".join(lines[1:-1]) if len(lines) > 2 else text
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        print(f"[iterate] plan JSON parse failed: {exc}; "
              f"first 500 chars: {raw_text[:500]!r}", flush=True)
        return IterationPlan([], [], [], rationale="Plan parse failed.")

    rationale = str(data.get("rationale", "")).strip()
    inv_paths = set(inventory.keys())

    raw_changes = data.get("changes") or []
    changes: list[IterationChange] = []
    for c in raw_changes[:_MAX_FILES_PER_ITERATION]:
        if not isinstance(c, dict): continue
        path = c.get("path")
        if not isinstance(path, str) or path not in inv_paths:
            print(f"[iterate] dropped change: {c} (path not in inventory)", flush=True)
            continue
        changes.append(IterationChange(
            path=path,
            reason=str(c.get("reason", ""))[:300],
        ))

    raw_new = data.get("new_files") or []
    new_files: list[SpecFile] = []
    for f in raw_new[:_MAX_FILES_PER_ITERATION]:
        if not isinstance(f, dict): continue
        path = f.get("path")
        if not isinstance(path, str) or path in inv_paths:
            print(f"[iterate] dropped new_file: {f} (path collision or invalid)", flush=True)
            continue
        new_files.append(SpecFile(
            path=path,
            purpose=str(f.get("purpose", ""))[:200],
            language=str(f.get("language", _language_of(path))),
            imports=[],  # We don't have these from the iteration plan;
                        # the file's content will be inspected by the
                        # import_extractor when it lands in the ledger.
            size_hint=str(f.get("size_hint", "medium"))[:50],
        ))

    raw_deletes = data.get("delete") or []
    deletes: list[str] = []
    for p in raw_deletes[:_MAX_FILES_PER_ITERATION]:
        if isinstance(p, str) and p in inv_paths:
            deletes.append(p)

    total = len(changes) + len(new_files) + len(deletes)
    if total > _MAX_FILES_PER_ITERATION:
        print(f"[iterate] plan touches {total} files; truncating to "
              f"{_MAX_FILES_PER_ITERATION}", flush=True)
        # Hard cap protects us against runaway plans.
        keep = _MAX_FILES_PER_ITERATION
        changes = changes[: max(0, keep)]
        keep -= len(changes)
        new_files = new_files[: max(0, keep)]
        keep -= len(new_files)
        deletes = deletes[: max(0, keep)]

    return IterationPlan(changes, new_files, deletes, rationale)


async def _regenerate_file(
    *,
    client: AnthropicClient,
    project_id: str,
    stage_tag: str,
    target_path: str,
    change_reason: str,
    iteration_prompt: str,
    current_content: str,
    inventory: dict[str, str],
    related_blobs: dict[str, str],
    related_paths: set[str],
    recorder: Optional[UsageRecorder],
    guardian_context: str = "",
) -> tuple[str, int, int]:
    """Ask the Builder to rewrite one file. Returns (new_content, in_tokens, out_tokens).

    `guardian_context` is an optional pre-formatted block of project-wide
    semantic summaries from the guardian indexer (see
    guardian_pipeline.build_pipeline_context). When present it's injected
    into the prompt right after the bare inventory so the Builder can
    reason about cross-file consequences without us loading every peer
    file's full content. Empty string when guardian indexing hasn't run
    for this project yet — prompt falls back to inventory-only context.
    """
    other_files = "\n".join(
        f"  - {p}: {s}" for p, s in sorted(inventory.items()) if p != target_path
    ) or "  (no other files)"

    # Include full content of related files (the other files this
    # iteration is also changing) so the regenerated file stays
    # consistent with them.
    related_blocks: list[str] = []
    for rp in sorted(related_paths):
        body = related_blobs.get(rp)
        if not body: continue
        related_blocks.append(
            f"\n--- CURRENT CONTENTS OF {rp} ---\n{body}\n--- END ---\n"
        )
    related_section = "".join(related_blocks) or "(no related files)"

    # Guardian context goes between the bare file list and the
    # full-content related files. The bare list anchors the Builder
    # in the project shape; guardian context tells it what each peer
    # actually does; related files give full detail for the ones
    # being co-edited. Each layer adds more depth on a narrower scope.
    guardian_section = (
        f"\n\n{guardian_context}\n" if guardian_context else ""
    )

    user_prompt = (
        f"User iteration request: {iteration_prompt}\n\n"
        f"Reason this file is changing: {change_reason}\n\n"
        f"--- CURRENT CONTENTS OF {target_path} ---\n"
        f"{current_content}\n"
        f"--- END ---\n\n"
        f"Other files in the project:\n{other_files}"
        f"{guardian_section}\n"
        f"Other files also being changed in this iteration:\n{related_section}\n\n"
        f"Output the complete new contents of `{target_path}`. "
        f"No markdown fences, no commentary."
    )

    result = await client.complete(
        user_prompt,
        system=REGEN_SYSTEM_PROMPT,
        max_tokens=8192,
    )
    if recorder is not None:
        recorder.record(
            project_id=project_id,
            provider=_PROVIDER,
            model=result.model,
            stage=f"{stage_tag}:file",
            subject=target_path,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )

    return _strip_fences(result.text), result.input_tokens, result.output_tokens


def _strip_fences(text: str) -> str:
    """Mirror of build_pipeline._strip_code_fences. Local copy so we
    don't create a cross-module dep cycle if either side changes."""
    t = text.strip()
    if t.startswith("```"):
        lines = t.splitlines()
        if len(lines) >= 2 and lines[-1].strip().startswith("```"):
            return "\n".join(lines[1:-1])
    return text


def _language_of(path: str) -> str:
    """Best-guess language tag from file extension. Used when the
    Builder doesn't specify one for a new file."""
    if path.endswith(".py"): return "python"
    if path.endswith((".ts", ".tsx")): return "typescript"
    if path.endswith((".js", ".jsx")): return "javascript"
    if path.endswith(".md"): return "markdown"
    if path.endswith((".yml", ".yaml")): return "yaml"
    if path.endswith(".json"): return "json"
    if path.endswith(".sql"): return "sql"
    if path.endswith(".sh"): return "bash"
    return "text"


def _write_outcome(
    store: LedgerStore,
    project_id: str,
    iteration_seq: int,
    outcome: IterationOutcome,
) -> None:
    store.write_entry(
        project_id=project_id,
        # Tier.GENERATION matches build_pipeline's outcome tier — the
        # iteration outcome IS a generation-phase summary record.
        # (Earlier draft used Tier.BUILD which doesn't exist; the valid
        # values are SPEC, GENERATION, MERGE, AUDIT, PATCH.)
        tier=Tier.GENERATION,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"iteration:{iteration_seq}:outcome",
        body={
            "iteration_seq": outcome.iteration_seq,
            "changes_applied": outcome.changes_applied,
            "new_files_created": outcome.new_files_created,
            "files_deleted": outcome.files_deleted,
            "failed": [{"path": p, "reason": r} for p, r in outcome.failed],
            "input_tokens": outcome.input_tokens,
            "output_tokens": outcome.output_tokens,
            "rationale": outcome.rationale,
            "completed_at": time.time(),
        },
        rationale=(
            f"Iteration {iteration_seq} complete: "
            f"{len(outcome.changes_applied)} changed, "
            f"{len(outcome.new_files_created)} new, "
            f"{len(outcome.files_deleted)} deleted, "
            f"{len(outcome.failed)} failed."
        ),
        author=f"worker:iterate:{iteration_seq}",
    )
