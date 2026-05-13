"""
codeflow.fix_all_pipeline
=========================

The "fix all issues" button. One pass:

  1. Read current audit_verdict entries from the ledger.
  2. Collect every finding (criticals, warnings, nits) — paths + line +
     issue text. Cap at FIX_ALL_MAX_FINDINGS to keep prompts under
     reasonable token budgets.
  3. Build an iteration prompt that lists everything and asks the
     Builder to address each issue or, if it cannot, to omit the
     change so the report step can explain.
  4. Run a normal iteration via iterate_pipeline.run_iteration. This
     produces a `fix_all:<seq>:iteration` ledger marker we can join on.
  5. Trigger an audit job for the post-fix state. The audit runs
     asynchronously; the report is generated when the post-fix audit
     completes (see handle_fix_all_finalize).

Why split fix and report
------------------------
Step 1-4 are bounded — single iteration, predictable cost. Step 5
(the post-fix audit) takes another 30-60 seconds and writes its own
verdict entries. The report step needs to compare PRE-fix findings
to POST-fix findings, which requires reading the audit's output.
Rather than make handle_fix_all block waiting for the audit job, we
chain: fix-all writes a "waiting-on-audit" marker, the audit handler
notices the marker and triggers report generation when done.

Cost shape
----------
Per fix-all on a 20-file project with ~30 findings: roughly one
iteration (~$0.55-0.85) + one full audit ($0.10) + one report call
($0.05). Total $0.70-1.00 typical, $2 worst case on a 100-file
project. Matches the estimate the frontend showed the user.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

from anthropic_client import AnthropicClient
from ledger import ArtifactKind, LedgerStore, Tier
from usage_recorder import UsageRecorder


# Hard cap on how many findings we'll cram into the prompt. Each
# finding is ~50-80 tokens; 100 findings = ~6-8K tokens just for the
# list, before we add file content context. This keeps us inside the
# Builder's effective working memory.
FIX_ALL_MAX_FINDINGS = 100


@dataclass(frozen=True)
class Finding:
    """One finding pulled out of an audit_verdict body."""
    file_path: str
    severity: str   # "critical" | "warning" | "nit"
    line: Optional[int]
    issue: str
    suggestion: str
    auditor: str


@dataclass(frozen=True)
class FixAllPlan:
    """What we send to the Builder for a fix-all pass."""
    seq: int
    findings: list[Finding]
    iteration_prompt: str
    truncated: bool  # True if we capped at FIX_ALL_MAX_FINDINGS


def collect_all_findings(
    store: LedgerStore, project_id: str,
    severities: tuple[str, ...] = ("critical", "warning", "nit"),
    max_findings: int = FIX_ALL_MAX_FINDINGS,
) -> tuple[list[Finding], bool]:
    """Read every current audit_verdict and flatten the findings.

    Returns (findings, truncated). De-duplicates across auditors:
    if openai and gemini both flag the same line of the same file
    with similar issue text, we keep the first occurrence. Strict
    de-dup is expensive (cross-auditor finding text varies); we
    accept some duplication.

    Truncation: if more than max_findings exist, we keep severity-
    first (criticals before warnings before nits) and return
    truncated=True so the caller can warn the user.
    """
    raw: list[Finding] = []
    try:
        verdicts = store.all_current(project_id, ArtifactKind.AUDIT_VERDICT)
    except Exception as exc:
        print(f"[fix-all] verdict read failed: {type(exc).__name__}: {exc}",
              flush=True)
        return [], False

    for v in verdicts:
        # Extract auditor name from artifact_key: ref:<pid>:audit:<auditor>:<path>
        key_parts = v.artifact_key.split(":")
        auditor = key_parts[3] if len(key_parts) >= 4 else "unknown"

        try:
            blob, _ = store.get_blob(v.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception:
            continue

        file_path = body.get("file_path") or ""
        findings = body.get("findings") or []
        if not isinstance(findings, list):
            continue
        for f in findings:
            if not isinstance(f, dict):
                continue
            sev = f.get("severity")
            if sev not in severities:
                continue
            raw.append(Finding(
                file_path=file_path,
                severity=sev,
                line=f.get("line") if isinstance(f.get("line"), int) else None,
                issue=(f.get("issue") or "").replace("\n", " ").strip()[:500],
                suggestion=(f.get("suggestion") or "").replace("\n", " ").strip()[:500],
                auditor=auditor,
            ))

    # Severity-first ordering: criticals, then warnings, then nits.
    sev_rank = {"critical": 0, "warning": 1, "nit": 2}
    raw.sort(key=lambda f: (sev_rank.get(f.severity, 99), f.file_path, f.line or 0))

    truncated = len(raw) > max_findings
    return raw[:max_findings], truncated


def estimate_fix_all_cost(
    store: LedgerStore, project_id: str,
) -> dict[str, Any]:
    """Quick estimate the frontend shows before the user confirms.

    Heuristic:
      - One Builder call to plan the fix (~5K input tokens at $3/Mtok = $0.015)
      - One Builder call per affected file with full content as context
        (~25K input + 1K output per file at Sonnet 4.6 prices)
      - One full re-audit of affected files (~$0.05-0.15)
      - One Builder call to generate the report (~$0.02)
    The estimate is intentionally rough — token counts vary widely with
    file size. We surface a range, not a precise number, so the user
    isn't surprised by either direction.
    """
    findings, truncated = collect_all_findings(store, project_id)
    if not findings:
        return {
            "issues_to_fix": 0,
            "files_affected": 0,
            "truncated": False,
            "estimated_cost_usd_low": 0.0,
            "estimated_cost_usd_high": 0.0,
        }
    files = {f.file_path for f in findings}
    files_count = len(files)

    # Per-file regeneration: input dominated by current file content +
    # other-file purposes. Output dominated by file size.
    # Sonnet 4.6: $3/Mtok in, $15/Mtok out.
    per_file_low = (15_000 * 3 / 1_000_000) + (800 * 15 / 1_000_000)
    per_file_high = (40_000 * 3 / 1_000_000) + (2_500 * 15 / 1_000_000)
    plan_cost = 0.02
    audit_cost_per_file_low = 0.005
    audit_cost_per_file_high = 0.015
    report_cost = 0.05

    low = (
        plan_cost
        + per_file_low * files_count
        + audit_cost_per_file_low * files_count
        + report_cost
    )
    high = (
        plan_cost
        + per_file_high * files_count
        + audit_cost_per_file_high * files_count
        + report_cost
    )

    return {
        "issues_to_fix": len(findings),
        "files_affected": files_count,
        "truncated": truncated,
        "by_severity": {
            "critical": sum(1 for f in findings if f.severity == "critical"),
            "warning": sum(1 for f in findings if f.severity == "warning"),
            "nit": sum(1 for f in findings if f.severity == "nit"),
        },
        "estimated_cost_usd_low": round(low, 2),
        "estimated_cost_usd_high": round(high, 2),
    }


def build_fix_all_prompt(findings: list[Finding], truncated: bool) -> str:
    """Construct the iteration prompt sent to the Builder.

    We group findings by file so the Builder sees them in context. Each
    finding has severity + line + issue + suggestion. We explicitly
    tell the Builder: if a finding can't be safely fixed by changing
    this file, skip it — the report step will explain.
    """
    by_file: dict[str, list[Finding]] = {}
    for f in findings:
        by_file.setdefault(f.file_path, []).append(f)

    blocks: list[str] = []
    for path in sorted(by_file.keys()):
        lines = [f"## {path}"]
        for f in by_file[path]:
            loc = f" (line {f.line})" if f.line else ""
            lines.append(f"- [{f.severity.upper()}]{loc} {f.issue}")
            if f.suggestion:
                lines.append(f"  Suggested fix: {f.suggestion}")
        blocks.append("\n".join(lines))

    prompt = (
        "Fix all the issues below that the auditors flagged. Make the "
        "smallest, safest change that addresses each issue. Do not "
        "rewrite functionality that wasn't flagged.\n\n"
        "If a finding cannot be safely fixed by editing this file "
        "(e.g. it requires architectural changes, depends on missing "
        "context, or fixing it would break other code), leave that "
        "specific issue alone and continue with the others. We will "
        "report any unfixed issues separately.\n\n"
        + "\n\n".join(blocks)
    )
    if truncated:
        prompt += (
            f"\n\nNOTE: more than {FIX_ALL_MAX_FINDINGS} findings exist; "
            "the list above was capped at the most severe ones. After "
            "this pass, audit will run again and surface what remains."
        )
    return prompt


async def generate_fix_all_report(
    *,
    project_id: str,
    fix_all_seq: int,
    pre_findings: list[Finding],
    post_findings: list[Finding],
    client: AnthropicClient,
    recorder: Optional[UsageRecorder],
) -> str:
    """Ask the Builder to explain remaining findings.

    We give it the list of issues that were present BEFORE the fix
    but are STILL present AFTER (i.e., issues the fix attempt didn't
    address) and the list of NEW findings (regressions). The Builder
    explains why each wasn't fixed or why it appeared.

    If everything is clean, we return a short success report without
    calling the Builder — saves a few cents and reads more naturally.
    """
    pre_keys = {_finding_key(f) for f in pre_findings}
    post_keys = {_finding_key(f) for f in post_findings}

    persisted = [f for f in post_findings if _finding_key(f) in pre_keys]
    regressions = [f for f in post_findings if _finding_key(f) not in pre_keys]
    fixed_count = len(pre_findings) - len(persisted)

    if not post_findings:
        return (
            f"Fix-all pass complete. All {len(pre_findings)} issue(s) "
            f"resolved. Audit is now clean."
        )

    # Build the "explain these" prompt.
    sections: list[str] = []
    if persisted:
        sections.append("## Issues that remain after the fix attempt:")
        for f in persisted[:30]:
            loc = f" line {f.line}" if f.line else ""
            sections.append(f"- [{f.severity}] {f.file_path}{loc}: {f.issue}")
    if regressions:
        sections.append("\n## New issues introduced by the fix attempt (regressions):")
        for f in regressions[:30]:
            loc = f" line {f.line}" if f.line else ""
            sections.append(f"- [{f.severity}] {f.file_path}{loc}: {f.issue}")

    explain_prompt = (
        "You just attempted to fix a set of code-review findings. Some "
        "remain. For each remaining issue below, briefly explain WHY "
        "it could not be safely fixed automatically — e.g. it requires "
        "architectural changes, depends on context outside this codebase, "
        "the fix would break other code, or it's a false positive from "
        "the auditor.\n\n"
        "Keep each explanation to one sentence. Use the format:\n"
        "  - `<file>` (line N): <one-sentence explanation>\n\n"
        + "\n".join(sections)
    )

    try:
        result = await client.complete(
            explain_prompt,
            system=(
                "You are a senior engineer explaining why automated "
                "fixes couldn't address specific code-review findings. "
                "Be direct. No filler."
            ),
            max_tokens=2048,
        )
        if recorder is not None:
            recorder.record(
                project_id=project_id, provider="anthropic",
                model=result.model, stage=f"fix_all:{fix_all_seq}:report",
                subject=None,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        explanation = result.text.strip()
    except Exception as exc:
        print(f"[fix-all] report generation failed: "
              f"{type(exc).__name__}: {exc}", flush=True)
        explanation = (
            "(Automated explanation unavailable — see the audit findings "
            "directly.)"
        )

    summary_lines = [
        f"Fix-all pass complete.",
        f"  Started with: {len(pre_findings)} issue(s)",
        f"  Fixed: {fixed_count}",
        f"  Remaining: {len(persisted)}",
    ]
    if regressions:
        summary_lines.append(f"  Regressions: {len(regressions)}")
    summary_lines.append("")
    summary_lines.append(explanation)
    return "\n".join(summary_lines)


def _finding_key(f: Finding) -> tuple[str, str, int, str]:
    """Stable identity for a finding so we can compare pre vs post.

    Includes file, severity, line, and the first 80 chars of issue
    text. We don't include auditor — if both auditors flagged the
    same thing pre-fix and only one still flags it post-fix, that
    still counts as "the issue persists" for the user.
    """
    return (
        f.file_path,
        f.severity,
        f.line or 0,
        (f.issue or "")[:80],
    )


def write_fix_all_started(
    store: LedgerStore, project_id: str, seq: int,
    issue_count: int, files_affected: int,
) -> None:
    """Marker that fix-all is running. UI can show this immediately
    so the user gets feedback before the iteration finishes."""
    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"fix_all:{seq}:started",
        body={
            "seq": seq,
            "issue_count": issue_count,
            "files_affected": files_affected,
            "started_at": time.time(),
        },
        rationale=(
            f"Fix-all pass {seq} started: {issue_count} issue(s) "
            f"across {files_affected} file(s)."
        ),
        author=f"worker:fix_all:{seq}",
    )


def write_fix_all_report(
    store: LedgerStore, project_id: str, seq: int,
    report: str, pre_count: int, post_count: int, fixed: int,
    regressions: int,
) -> None:
    """Final outcome of a fix-all pass."""
    store.write_entry(
        project_id=project_id,
        tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"fix_all:{seq}:report",
        body={
            "seq": seq,
            "pre_count": pre_count,
            "post_count": post_count,
            "fixed": fixed,
            "regressions": regressions,
            "report": report,
            "completed_at": time.time(),
        },
        rationale=(
            f"Fix-all pass {seq} complete: fixed {fixed}, "
            f"{post_count} remain"
            + (f", {regressions} regression(s)" if regressions else "")
            + "."
        ),
        author=f"worker:fix_all:{seq}",
    )


def next_fix_all_seq(store: LedgerStore, project_id: str) -> int:
    """Compute the next fix_all sequence number from existing markers."""
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    started = sum(
        1 for d in decisions
        if d.artifact_key.startswith("fix_all:")
        and d.artifact_key.endswith(":started")
    )
    return started + 1
