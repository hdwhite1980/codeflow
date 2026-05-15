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

import hashlib
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

    NOTE: this is the legacy single-prompt builder. The handler now
    prefers cluster_findings_by_topic + build_cluster_prompt which
    produces N smaller prompts with per-cluster memory context. We
    keep this one for fallback (no memory available) and back-compat.
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


# ---------------------------------------------------------------------------
# Memory-aware + cluster-aware fix-all (Turn fix-all-A+B).
# ---------------------------------------------------------------------------
#
# Why this exists
# ---------------
# The legacy build_fix_all_prompt sends every finding in one giant prompt
# without any guardian context. Three failure modes that produces:
#   1. Builder doesn't know that fixing file X requires coordinated changes
#      in file Y. Result: regressions.
#   2. Multiple related findings (e.g. 6 schema-validation bugs in store.js)
#      get fixed inconsistently — one with throw, another with default,
#      another with silent drop. Result: incoherent file.
#   3. No risk_notes context, so the Builder doesn't know which existing
#      patterns to preserve.
#
# The new flow:
#   - Cluster findings by topic-token overlap (same algorithm as ambient
#     review). Issues that share concept (e.g. "schema validation",
#     "rate limiting", "error swallowing") cluster together.
#   - For each cluster, look up guardian summaries for every file in
#     the cluster, plus their direct dependents.
#   - Build a per-cluster prompt that includes both the findings and
#     the memory context.
#   - Run one iteration per cluster. Each iteration is smaller, more
#     focused, and gets dedicated context.
#
# Tradeoffs:
#   - N iterations instead of 1 means N Builder calls (~$). For a typical
#     fix-all with 4-6 clusters that's a 4-6x cost increase per pass.
#   - But: each iteration is smaller (fewer files in context), more
#     accurate (memory context tells the Builder about consequences),
#     and less likely to regress. Net cost over time should be lower
#     because we don't have to run fix-all repeatedly to clean up
#     regressions from the prior fix-all.

@dataclass(frozen=True)
class FindingCluster:
    """Group of related findings to be fixed in one Builder call.

    Clusters are identified by topic_key — a stable identifier derived
    from token overlap of the issues. Files within a cluster are the
    union of file_paths across all member findings; dependents are the
    set of files that import/depend on those (extracted from the graph
    by the caller, not stored here)."""
    topic_key: str
    topic_label: str           # short human-readable label for logs/UI
    findings: list["Finding"]
    file_paths: list[str]      # union of file_paths from findings


def cluster_findings_by_topic(
    findings: list["Finding"],
    *,
    max_clusters: int = 8,
) -> list[FindingCluster]:
    """Group findings into clusters that should be fixed together.

    Topic key derivation matches ambient_review:
      - Tokenize the issue text into meaningful words (stopwords removed)
      - Use sorted token set as the cluster key
      - Findings whose tokens have substantial overlap join the same
        cluster

    Approach: use ambient_review's _topic_tokens/_topic_key for stable
    behavior across the codebase. Then merge clusters whose token sets
    have >=50% overlap — catches near-duplicates like "missing input
    validation on auth endpoint" + "no input validation on admin endpoint"
    that have only one differing token.

    Returns clusters sorted by max-severity within cluster (critical-first)
    then by file count, capped at max_clusters. Findings that don't fit
    anywhere become a final "miscellaneous" cluster.
    """
    from ambient_review import _topic_tokens, _topic_key

    # Stage 1: initial bucketing by exact topic key
    buckets: dict[str, list[tuple["Finding", list[str]]]] = {}
    for f in findings:
        # Combine issue + suggestion for richer token signal
        text = f"{f.issue or ''} {f.suggestion or ''}".strip()
        tokens = _topic_tokens(text)
        if not tokens:
            # Findings with no meaningful tokens (rare — would be all
            # stopwords or empty). Group them under their file path so
            # they at least share a Builder context.
            key = f"misc:{f.file_path}"
        else:
            key = _topic_key(tokens)
        buckets.setdefault(key, []).append((f, tokens))

    # Stage 2: merge buckets with high token overlap (>=50%). Walk in
    # descending size order so we extend the big clusters first rather
    # than chasing small ones together.
    sorted_keys = sorted(buckets.keys(), key=lambda k: -len(buckets[k]))
    merged: dict[str, list[tuple["Finding", list[str]]]] = {}
    consumed: set[str] = set()

    for primary_key in sorted_keys:
        if primary_key in consumed:
            continue
        primary_tokens = set()
        for _, toks in buckets[primary_key]:
            primary_tokens.update(toks)

        members = list(buckets[primary_key])
        consumed.add(primary_key)

        # Find other buckets to merge in.
        for other_key in sorted_keys:
            if other_key in consumed:
                continue
            if other_key.startswith("misc:") or primary_key.startswith("misc:"):
                continue  # don't merge misc buckets into real ones
            other_tokens = set()
            for _, toks in buckets[other_key]:
                other_tokens.update(toks)
            if not other_tokens or not primary_tokens:
                continue
            overlap = primary_tokens & other_tokens
            smaller_size = min(len(primary_tokens), len(other_tokens))
            if smaller_size > 0 and len(overlap) / smaller_size >= 0.5:
                members.extend(buckets[other_key])
                consumed.add(other_key)
                primary_tokens.update(other_tokens)

        merged[primary_key] = members

    # Stage 3: build FindingCluster objects.
    severity_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    clusters: list[FindingCluster] = []
    for key, members in merged.items():
        cluster_findings = [m[0] for m in members]
        file_paths = sorted({f.file_path for f in cluster_findings})

        # Topic label: best-effort human-readable summary. Pick the
        # 2-3 most distinctive tokens from the most severe finding.
        worst = min(
            cluster_findings,
            key=lambda f: severity_rank.get(f.severity, 5),
        )
        worst_tokens = next(
            (toks for fnd, toks in members if fnd is worst),
            [],
        )
        # Filter out file-name and path-context tokens before labeling.
        label_tokens = [
            t for t in worst_tokens[:5]
            if t not in {"file", "files", "function", "method", "class"}
        ]
        topic_label = " ".join(label_tokens[:3]) if label_tokens else "miscellaneous"

        clusters.append(FindingCluster(
            topic_key=key,
            topic_label=topic_label,
            findings=cluster_findings,
            file_paths=file_paths,
        ))

    # Sort: highest-severity first, then most files affected.
    def _cluster_sort_key(c: FindingCluster):
        worst_sev = min(
            severity_rank.get(f.severity, 5) for f in c.findings
        )
        return (worst_sev, -len(c.file_paths), -len(c.findings))

    clusters.sort(key=_cluster_sort_key)
    return clusters[:max_clusters]


def build_cluster_prompt(
    cluster: FindingCluster,
    *,
    cluster_index: int,
    total_clusters: int,
    guardian_context: str = "",
    dependent_paths: list[str] | None = None,
) -> str:
    """Build the Builder prompt for one cluster, with memory context.

    The prompt is structured to make consequences explicit:
      1. Identify what concept the cluster is about
      2. List the findings (issue + suggestion + severity + line)
      3. Inject guardian semantic summaries for the target files
      4. List dependent files explicitly: "fixing X must not break Y"
      5. Tell the Builder to make coordinated changes consistently

    `guardian_context` is the output of build_pipeline_context_with_paths
    pre-filtered to relevant files. Empty string is fine — the prompt
    still works without it but lacks the memory layer.
    """
    lines: list[str] = []
    lines.append(
        f"Fix cluster {cluster_index + 1} of {total_clusters}: "
        f"{cluster.topic_label}"
    )
    lines.append("")
    lines.append(
        f"The following {len(cluster.findings)} finding"
        f"{'s' if len(cluster.findings) != 1 else ''} share a common "
        "concept. They MUST be fixed with a CONSISTENT approach — "
        "do not apply different patterns to different findings in "
        "this cluster. Decide the right fix once, then apply it "
        "everywhere it's relevant within the files listed."
    )
    lines.append("")

    # Findings grouped by file for readability.
    by_file: dict[str, list["Finding"]] = {}
    for f in cluster.findings:
        by_file.setdefault(f.file_path, []).append(f)

    lines.append("## Findings to fix")
    for path in sorted(by_file.keys()):
        lines.append(f"\n### {path}")
        for f in by_file[path]:
            loc = f" (line {f.line})" if f.line else ""
            lines.append(f"- [{f.severity.upper()}]{loc} {f.issue}")
            if f.suggestion:
                lines.append(f"  Suggested fix: {f.suggestion}")

    if guardian_context:
        lines.append("")
        lines.append("## Guardian semantic context")
        lines.append(
            "The following memory was indexed before this fix attempt. "
            "Use it to understand the existing patterns and avoid "
            "breaking dependent code:"
        )
        lines.append("")
        lines.append(guardian_context)

    if dependent_paths:
        lines.append("")
        lines.append("## Files that depend on what you're changing")
        lines.append(
            "These files are NOT being edited in this pass, but they "
            "depend on the files you are editing. Your fixes must "
            "remain compatible with them:"
        )
        for p in dependent_paths:
            lines.append(f"  - {p}")

    lines.append("")
    lines.append(
        "If a finding cannot be safely fixed without changes to files "
        "outside this cluster — leave that specific finding alone and "
        "continue with the others. Do not produce a half-fix that "
        "leaves the codebase in a worse state. Unfixed findings will "
        "be reported separately."
    )
    return "\n".join(lines)


def discover_dependent_paths(
    store: "LedgerStore", project_id: str, target_paths: list[str],
) -> list[str]:
    """Return files that depend on any of `target_paths` via the
    project's graph edges. Used to warn the Builder which downstream
    files need to remain compatible with its changes.

    Falls back gracefully on graph errors — empty list means "we don't
    know what depends on this, proceed without that hint."
    """
    if not target_paths:
        return []
    targets = set(target_paths)
    dependents: set[str] = set()
    try:
        for path in target_paths:
            try:
                neighbors = store.neighbors(
                    project_id, f"file:{project_id}:{path}",
                )
            except Exception:
                continue
            # neighbors() returns inbound edges (who points at me).
            for n in neighbors or []:
                key = getattr(n, "artifact_key", "")
                parts = key.split(":", 2)
                if len(parts) >= 3 and parts[0] == "file":
                    dep = parts[2]
                    if dep not in targets:
                        dependents.add(dep)
    except Exception as exc:
        print(f"[fix_all] discover_dependent_paths failed: "
              f"{type(exc).__name__}: {exc}", flush=True)
        return []
    return sorted(dependents)


async def generate_fix_all_report(
    *,
    project_id: str,
    fix_all_seq: int,
    pre_findings: list[Finding],
    post_findings: list[Finding],
    client: AnthropicClient,
    recorder: Optional[UsageRecorder],
    store: Optional["LedgerStore"] = None,
) -> str:
    """Ask the Builder to explain remaining findings.

    We give it the list of issues that were present BEFORE the fix
    but are STILL present AFTER (i.e., issues the fix attempt didn't
    address) and the list of NEW findings (regressions). The Builder
    explains why each wasn't fixed or why it appeared.

    When `store` is provided AND the project has guardian semantic
    summaries, we include them as context so the Builder's
    explanations can reference cross-file consequences — e.g. "this
    can't be fixed in auth.py alone because the summary of db.py
    shows tenant scoping isn't enforced there." Without the store
    argument the function behaves exactly as before (back-compat).

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

    # Pull guardian semantic context for the files involved if available.
    # Focus on the files that have remaining or regressed issues — those
    # are the ones the Builder is reasoning about. Empty string when
    # guardian hasn't indexed this project (and on stores that don't
    # support load_file_summaries, e.g. test doubles).
    guardian_context = ""
    if store is not None:
        try:
            from guardian_pipeline import build_pipeline_context
            involved = sorted({f.file_path for f in (persisted + regressions)})
            guardian_context = build_pipeline_context(
                store, project_id, focus_paths=involved,
            )
        except Exception as exc:
            # Guardian failure must not break fix-all report generation.
            # Log and continue without semantic context.
            print(f"[fix-all] guardian context unavailable: "
                  f"{type(exc).__name__}: {exc}", flush=True)

    guardian_block = f"\n\n{guardian_context}\n" if guardian_context else ""

    explain_prompt = (
        "You just attempted to fix a set of code-review findings. Some "
        "remain. For each remaining issue below, briefly explain WHY "
        "it could not be safely fixed automatically — e.g. it requires "
        "architectural changes, depends on context outside this codebase, "
        "the fix would break other code, or it's a false positive from "
        "the auditor.\n\n"
        "Keep each explanation to one sentence. Use the format:\n"
        "  - `<file>` (line N): <one-sentence explanation>\n"
        + guardian_block + "\n"
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


def write_fix_all_resolved(
    store: LedgerStore, project_id: str, seq: int,
    resolved_findings: list[Finding],
) -> None:
    """Persist the list of findings that THIS pass successfully resolved.

    Used by the next pass's oscillation filter (Fix B). Each entry
    captures path + issue text + line. Auditor name is intentionally
    omitted — oscillation can be cross-auditor (one auditor "fixed" a
    finding, the other auditor flips it on the next pass) and we want
    to suppress that case.

    Bounded size: cap at 200 entries to keep the ledger row reasonable.
    """
    store.write_entry(
        project_id=project_id,
        tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"fix_all:{seq}:resolved",
        body={
            "seq": seq,
            "resolved": [
                {
                    "file_path": f.file_path,
                    "issue": f.issue,
                    "line": f.line,
                    "severity": f.severity,
                }
                for f in resolved_findings[:200]
            ],
            "resolved_at": time.time(),
        },
        rationale=f"Fix-all pass {seq} resolved {len(resolved_findings)} finding(s)",
        author=f"worker:fix_all:{seq}",
    )


def load_recent_resolved_findings(
    store: LedgerStore, project_id: str, *, lookback_passes: int = 2,
) -> list[dict[str, Any]]:
    """Return findings resolved by the last `lookback_passes` fix-all runs.

    Used to detect oscillation: if a finding very similar to one of
    these shows up in the next audit, it's probably the system flipping
    the same property back and forth across passes, not a real new bug.

    We default to lookback=2 because 1-pass oscillation is the common
    case (A→B on pass N, B→A on pass N+1). 3+ passes get into territory
    where actual code drift is plausible, so we don't suppress those.
    """
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    resolved_records: list[tuple[int, dict[str, Any]]] = []
    for d in decisions:
        if not d.artifact_key.startswith("fix_all:"):
            continue
        if not d.artifact_key.endswith(":resolved"):
            continue
        try:
            blob, _ = store.get_blob(d.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception:
            continue
        seq = int(body.get("seq", 0))
        resolved_records.append((seq, body))

    # Take the most recent N passes.
    resolved_records.sort(key=lambda x: -x[0])
    out: list[dict[str, Any]] = []
    for seq, body in resolved_records[:lookback_passes]:
        for entry in body.get("resolved", []):
            if isinstance(entry, dict):
                out.append(entry)
    return out


def _normalize_issue_text(text: str) -> set[str]:
    """Tokenize an issue into a set of meaningful words for similarity
    comparison. Reuses ambient_review's stopword list for consistency.
    Lowercased, stopwords removed, tokens shorter than 3 chars dropped."""
    try:
        from ambient_review import _topic_tokens
        return set(_topic_tokens(text))
    except Exception:
        # Defensive fallback: simple lowercased split.
        return {t.lower() for t in text.split() if len(t) > 2}


def _findings_overlap_threshold(text_a: str, text_b: str) -> float:
    """Jaccard similarity of meaningful tokens. 1.0 = identical token
    sets, 0.0 = no overlap. >=0.6 is "very likely the same concern."

    Why Jaccard instead of exact match: auditors paraphrase. "Hard-coded
    API key" and "Hardcoded API token in source" describe the same bug
    but won't match exactly. Token-set overlap catches it."""
    a = _normalize_issue_text(text_a)
    b = _normalize_issue_text(text_b)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def filter_oscillating_findings(
    findings: list[Finding],
    recent_resolved: list[dict[str, Any]],
    *,
    similarity_threshold: float = 0.45,
) -> tuple[list[Finding], list[Finding]]:
    """Drop findings that look like the inverse of a recently-resolved one.

    Returns (kept_findings, suppressed_findings). Suppression is per-file:
    a newly-flagged finding on a.py is checked against resolved findings
    on a.py only.

    Threshold = 0.45 Jaccard on meaningful tokens. Tuned from production
    cases where typical oscillations score 0.4–0.6 on shared concept
    tokens (file/line/issue context produces lexical drift that 0.6
    misses but 0.3 lets through). 0.45 catches the README.md flip-flop
    pattern without trapping plausibly-distinct findings.

    Why filter rather than re-prioritize: if pass N "fixed" a thing and
    pass N+1 immediately flags it again with similar text, the Builder
    is going to undo pass N's work. That's worse than silence — it
    produces the visible "21 fixed / 19 regressions" pattern. Better to
    surface the oscillation as a "needs decision" item (which Fix C
    does for the cross-auditor case) than auto-act.
    """
    if not recent_resolved:
        return list(findings), []

    # Index recent resolved by file_path for cheap lookup.
    by_file: dict[str, list[dict[str, Any]]] = {}
    for entry in recent_resolved:
        path = entry.get("file_path") or ""
        if path:
            by_file.setdefault(path, []).append(entry)

    kept: list[Finding] = []
    suppressed: list[Finding] = []
    for f in findings:
        candidates = by_file.get(f.file_path, [])
        is_oscillation = False
        for prior in candidates:
            prior_issue = prior.get("issue") or ""
            score = _findings_overlap_threshold(f.issue, prior_issue)
            if score >= similarity_threshold:
                is_oscillation = True
                print(f"[fix-all] suppressing likely oscillation: "
                      f"{f.file_path} '{f.issue[:80]}' "
                      f"(overlap={score:.2f} with prior '{prior_issue[:80]}')",
                      flush=True)
                break
        if is_oscillation:
            suppressed.append(f)
        else:
            kept.append(f)
    return kept, suppressed


# ---------------------------------------------------------------------------
# Auditor-disagreement detection (Fix C).
# ---------------------------------------------------------------------------
#
# Two auditors (openai + gemini) flagging the SAME file at the SAME line
# with CONTRADICTORY suggestions is a strong signal that the question is
# judgment-dependent. Examples we've seen in production:
#   * One auditor says "remove cross-platform claim, require PS 5.1"
#   * Other auditor says "add cross-platform support, claim PS 7+"
# Both findings get queued and fix-all flips the file each pass.
#
# Detection strategy: group findings by (file_path, line_bucket). If
# multiple auditors contributed AND their suggestion tokens don't
# overlap meaningfully (Jaccard < 0.3), we have disagreement. Those
# findings get pulled out of the auto-fix queue and surfaced separately.

# Window size for "same line" — strict equality misses cases where
# one auditor flagged line 42 and another flagged line 43 for what's
# clearly the same code region. 5 lines is generous without being so
# loose that unrelated findings collide.
_DISAGREEMENT_LINE_WINDOW = 5

# Jaccard threshold for "suggestions don't overlap." We accept some
# token overlap on incidental words (the, file, function, etc. that
# survived stopword filtering) — true disagreement is when the
# suggestions describe different actions.
_DISAGREEMENT_OVERLAP_MAX = 0.3


def detect_auditor_disagreements(
    findings: list[Finding],
) -> tuple[list[Finding], list[list[Finding]]]:
    """Split findings into (consensus_findings, disagreement_groups).

    A "disagreement group" is a set of findings on the same file region
    flagged by different auditors with contradictory suggestions. These
    are returned separately so the caller can:
      - Skip them in fix-all (don't auto-act on judgment-dependent issues)
      - Surface them to the user as a "needs decision" item

    Detection is conservative: we only mark a group as disagreement
    when there are findings from DIFFERENT auditors. Single-auditor
    duplicates pass through normally.
    """
    if not findings:
        return [], []

    # Bucket by (file_path, line // window). Findings within the same
    # window on the same file are candidates for disagreement clustering.
    buckets: dict[tuple[str, int], list[Finding]] = {}
    for f in findings:
        line_bucket = (f.line or 0) // _DISAGREEMENT_LINE_WINDOW
        key = (f.file_path, line_bucket)
        buckets.setdefault(key, []).append(f)

    consensus: list[Finding] = []
    disagreement_groups: list[list[Finding]] = []
    suppressed: set[int] = set()  # id() of findings already grouped

    for key, group in buckets.items():
        if len(group) < 2:
            consensus.extend(group)
            continue

        # Find pairs from different auditors.
        disagreeing: list[Finding] = []
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                fi, fj = group[i], group[j]
                if fi.auditor == fj.auditor:
                    continue
                # Compare suggestion tokens. If suggestions are empty,
                # fall back to issue text.
                a_text = fi.suggestion or fi.issue
                b_text = fj.suggestion or fj.issue
                overlap = _findings_overlap_threshold(a_text, b_text)
                if overlap < _DISAGREEMENT_OVERLAP_MAX:
                    if id(fi) not in suppressed:
                        disagreeing.append(fi)
                        suppressed.add(id(fi))
                    if id(fj) not in suppressed:
                        disagreeing.append(fj)
                        suppressed.add(id(fj))

        if disagreeing:
            disagreement_groups.append(disagreeing)

        # Anything in the bucket not flagged as disagreeing is consensus.
        for f in group:
            if id(f) not in suppressed:
                consensus.append(f)

    return consensus, disagreement_groups


def _disagreement_digest(group: list[Finding]) -> str:
    """Stable hash for a disagreement group. Same disagreement re-
    emerging in a later pass produces the same digest, which means
    resolution state survives across passes.

    Inputs to the hash:
      - file path (the disagreement is file-scoped)
      - lowest line number in the group (collapses adjacent-line cases)
      - sorted set of (auditor, issue) tuples (who said what, content-
        identifying)

    Suggestion text intentionally excluded because the model may
    paraphrase suggestions slightly across passes while still raising
    the same underlying concern.
    """
    if not group:
        return ""
    file_path = group[0].file_path or ""
    min_line = min((f.line or 0) for f in group)
    parts = sorted({(f.auditor, (f.issue or "").strip()[:200]) for f in group})
    seed = f"{file_path}::{min_line}::" + "||".join(
        f"{a}:{i}" for a, i in parts
    )
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def write_audit_disagreements(
    store: LedgerStore, project_id: str, seq: int,
    disagreement_groups: list[list[Finding]],
) -> None:
    """Persist auditor disagreements as ledger records the UI surfaces
    as "needs human decision" items.

    Each group writes its OWN artifact under
    ``audit_disagreement:<project>:<digest>``. Digest is stable across
    passes (same disagreement re-emerging upserts in place), so
    resolution state survives indexer re-runs.

    Empty groups list is a no-op — we don't want to clutter the ledger
    with empty markers.
    """
    if not disagreement_groups:
        return
    for grp in disagreement_groups:
        digest = _disagreement_digest(grp)
        if not digest:
            continue
        body = {
            "digest": digest,
            "seq": seq,
            "file_path": grp[0].file_path if grp else "",
            "line": min((f.line or 0) for f in grp) if grp else None,
            "findings": [
                {
                    "auditor": f.auditor,
                    "severity": f.severity,
                    "line": f.line,
                    "issue": f.issue,
                    "suggestion": f.suggestion,
                }
                for f in grp
            ],
            "detected_at": time.time(),
            "resolved_at": None,
            "resolved_auditor": None,
            "resolved_action": None,  # "queue_fix" | "dismiss_both"
        }
        # If a resolution exists from a prior pass, preserve it. Same
        # logic as ambient findings' dismissal preservation.
        target_key = f"audit_disagreement:{project_id}:{digest}"
        previous = store.current_entry(project_id, target_key)
        if previous is not None:
            try:
                prev_blob, _ = store.get_blob(previous.blob_sha256)
                prev_body = json.loads(prev_blob.decode("utf-8")) if prev_blob else {}
                if prev_body.get("resolved_at"):
                    body["resolved_at"] = prev_body.get("resolved_at")
                    body["resolved_auditor"] = prev_body.get("resolved_auditor")
                    body["resolved_action"] = prev_body.get("resolved_action")
            except Exception:
                pass
        store.write_entry(
            project_id=project_id,
            tier=Tier.AUDIT,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key=target_key,
            body=body,
            rationale=(
                f"Auditor disagreement on {body['file_path']}"
                + (f" line {body['line']}" if body['line'] else "")
                + f" between {' vs '.join(sorted({f.auditor for f in grp}))}"
            ),
            author=f"worker:fix_all:{seq}",
        )


def list_audit_disagreements(
    store: LedgerStore, project_id: str, *,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    """Return current disagreements for a project.

    Unresolved-only by default. Use ``include_resolved=True`` for an
    audit-log view.

    Sorted: unresolved first, then by detected_at descending.
    """
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    out: list[dict[str, Any]] = []
    prefix = f"audit_disagreement:{project_id}:"
    for d in decisions:
        if not d.artifact_key.startswith(prefix):
            continue
        try:
            blob, _ = store.get_blob(d.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception:
            continue
        if not include_resolved and body.get("resolved_at"):
            continue
        out.append(body)

    out.sort(key=lambda b: (
        b.get("resolved_at") is not None,  # unresolved first (False sorts before True)
        -(b.get("detected_at") or 0),
    ))
    return out


def resolve_audit_disagreement(
    store: LedgerStore, project_id: str, digest: str,
    *,
    action: str,
    chosen_auditor: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Record a user's resolution of an auditor disagreement.

    Two valid actions:
      - ``"queue_fix"``: user picked one auditor's finding as correct.
        ``chosen_auditor`` must be set. The selected finding gets
        returned so the caller can re-queue it via the normal fix-all
        flow. The other auditor's finding is implicitly dismissed.
      - ``"dismiss_both"``: user decided neither auditor was right
        (e.g. both findings are opinion-based). Both findings dropped.

    Returns the chosen finding body (dict) when action=queue_fix, or
    None for dismiss_both or when the digest isn't found.

    The disagreement record's resolved_* fields are stamped; the entry
    is superseded with the new body. Future fix-all passes that
    re-detect this disagreement see the prior resolution and preserve
    the dismissal/queue state.
    """
    if action not in {"queue_fix", "dismiss_both"}:
        raise ValueError(
            f"action must be 'queue_fix' or 'dismiss_both', got {action!r}"
        )
    if action == "queue_fix" and not chosen_auditor:
        raise ValueError("queue_fix requires chosen_auditor")

    target_key = f"audit_disagreement:{project_id}:{digest}"
    current = store.current_entry(project_id, target_key)
    if current is None:
        return None
    try:
        blob, _ = store.get_blob(current.blob_sha256)
        body = json.loads(blob.decode("utf-8")) if blob else {}
    except Exception:
        return None

    chosen_finding: Optional[dict[str, Any]] = None
    if action == "queue_fix":
        for f in body.get("findings", []):
            if f.get("auditor") == chosen_auditor:
                chosen_finding = f
                break
        if chosen_finding is None:
            raise ValueError(
                f"no finding from auditor {chosen_auditor!r} in this group"
            )

    body["resolved_at"] = time.time()
    body["resolved_auditor"] = chosen_auditor
    body["resolved_action"] = action

    store.write_entry(
        project_id=project_id,
        tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=target_key,
        body=body,
        rationale=(
            f"User resolved disagreement on {body.get('file_path', '?')}: "
            + (f"chose {chosen_auditor}'s position"
               if action == "queue_fix" else "dismissed both auditors")
        ),
        author="user:resolve_disagreement",
    )
    return chosen_finding


def loaded_resolved_disagreements_as_findings(
    store: LedgerStore, project_id: str,
) -> list[Finding]:
    """Return Finding objects from disagreements the user resolved with
    ``queue_fix`` action. Caller adds these back into the next fix-all
    pass's findings list so the chosen-auditor finding actually gets
    fixed.

    Once a resolution-queued finding has been acted upon — i.e. it
    appears in the resolved set of a later fix-all pass — it stops
    being re-queued. We detect this by checking whether the resolution
    timestamp predates the most recent fix-all completion.
    """
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    prefix = f"audit_disagreement:{project_id}:"

    # Find the most recent fix-all completion to know whether resolutions
    # have been acted on. If a resolution timestamp is OLDER than the
    # most recent fix-all report, the chosen finding has had its shot.
    most_recent_report_at = 0.0
    for d in decisions:
        if (d.artifact_key.startswith("fix_all:")
                and d.artifact_key.endswith(":report")):
            try:
                blob, _ = store.get_blob(d.blob_sha256)
                body = json.loads(blob.decode("utf-8")) if blob else {}
                completed = float(body.get("completed_at") or 0)
                if completed > most_recent_report_at:
                    most_recent_report_at = completed
            except Exception:
                continue

    out: list[Finding] = []
    for d in decisions:
        if not d.artifact_key.startswith(prefix):
            continue
        try:
            blob, _ = store.get_blob(d.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception:
            continue
        if body.get("resolved_action") != "queue_fix":
            continue
        resolved_at = float(body.get("resolved_at") or 0)
        if resolved_at <= most_recent_report_at:
            # The resolution predates the most recent fix-all completion
            # — it's already had its chance to run. Don't re-queue.
            continue
        chosen_auditor = body.get("resolved_auditor")
        for f in body.get("findings", []):
            if f.get("auditor") == chosen_auditor:
                out.append(Finding(
                    file_path=body.get("file_path", ""),
                    severity=f.get("severity", "warning"),
                    line=f.get("line"),
                    issue=f.get("issue", ""),
                    suggestion=f.get("suggestion", ""),
                    auditor=f.get("auditor", "unknown"),
                ))
                break
    return out


def next_fix_all_seq(store: LedgerStore, project_id: str) -> int:
    """Compute the next fix_all sequence number from existing markers."""
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    started = sum(
        1 for d in decisions
        if d.artifact_key.startswith("fix_all:")
        and d.artifact_key.endswith(":started")
    )
    return started + 1


# ---------------------------------------------------------------------------
# Decisions-needed surface (Fix D + Fix F).
# ---------------------------------------------------------------------------
#
# Why this exists
# ---------------
# A class of audit findings can't be auto-fixed because the right answer
# requires human input — fake URLs in a manifest, mandatory-vs-optional
# dependency declarations, which exception type a function should raise.
# When the Builder is asked to fix one of these, it can do one of three
# things:
#   1. Invent a value (bad — produces fake-looking output)
#   2. Silently leave it unfixed (bad — looks like a regression next pass)
#   3. Emit a structured refusal listing what decision is needed (this)
#
# Storage shape
# -------------
# Each pending decision lives under
#   decision_needed:<project_id>:<digest>
# Digest is stable over (file_path, issue_text), so the same Builder
# refusal across multiple fix-all passes upserts in place. Body fields:
#   - digest, file_path, line, issue, decision_needed, decision_type,
#     blocking_info, detected_at, resolved_at, resolved_value
#
# Resolution flow
# ---------------
# User opens the decisions panel, reads the question, provides a value
# or chooses "leave as-is." On `provide_value`, we synthesize a Finding
# with the user's answer woven into the suggestion, and re-inject it
# into the next fix-all pass via the same path used for resolved
# disagreements (loaded_resolved_disagreements_as_findings has an
# analogue here). On `dismiss`, we mark the finding as won't-fix and
# suppress future audits flagging it (out of scope for this turn;
# noted for follow-up).

def _decision_digest(file_path: str, issue: str) -> str:
    """Stable hash over file + issue text. Same Builder refusal across
    runs gets the same digest so resolution state survives."""
    seed = f"{file_path or ''}::{(issue or '')[:300]}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def write_decisions_needed(
    store: LedgerStore, project_id: str, seq: int,
    unfixable_findings: list[dict[str, Any]],
) -> None:
    """Persist a list of Builder-flagged unfixable findings.

    Each entry writes its own artifact under
    ``decision_needed:<project>:<digest>``. Prior resolution state on
    the same digest is preserved if present.

    Empty list is a no-op — fix-all may run many passes with nothing
    to surface, and we don't want to clutter the ledger with empty
    markers.
    """
    if not unfixable_findings:
        return
    for u in unfixable_findings:
        if not isinstance(u, dict):
            continue
        file_path = u.get("file_path", "")
        issue = u.get("issue", "")
        if not file_path or not issue:
            continue
        digest = _decision_digest(file_path, issue)
        body = {
            "digest": digest,
            "seq": seq,
            "file_path": file_path,
            "line": u.get("line"),
            "issue": issue,
            "decision_needed": u.get("decision_needed", ""),
            "decision_type": u.get("decision_type", "policy"),
            "blocking_info": u.get("blocking_info", ""),
            "detected_at": time.time(),
            "resolved_at": None,
            "resolved_value": None,
            "resolved_action": None,  # "provide_value" | "dismiss"
        }
        target_key = f"decision_needed:{project_id}:{digest}"
        previous = store.current_entry(project_id, target_key)
        if previous is not None:
            try:
                prev_blob, _ = store.get_blob(previous.blob_sha256)
                prev_body = json.loads(prev_blob.decode("utf-8")) if prev_blob else {}
                if prev_body.get("resolved_at"):
                    body["resolved_at"] = prev_body["resolved_at"]
                    body["resolved_value"] = prev_body.get("resolved_value")
                    body["resolved_action"] = prev_body.get("resolved_action")
            except Exception:
                pass
        store.write_entry(
            project_id=project_id,
            tier=Tier.AUDIT,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key=target_key,
            body=body,
            rationale=(
                f"Builder declined to fix {file_path}"
                + (f" line {u.get('line')}" if u.get("line") else "")
                + f": {body['decision_needed'][:140]}"
            ),
            author=f"worker:fix_all:{seq}",
        )


def list_decisions_needed(
    store: LedgerStore, project_id: str, *,
    include_resolved: bool = False,
) -> list[dict[str, Any]]:
    """Return current pending decisions for a project.

    Sorted: unresolved first (by decision_type priority — architectural
    decisions surface before value/policy), then by detected_at desc.
    """
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    prefix = f"decision_needed:{project_id}:"
    out: list[dict[str, Any]] = []
    for d in decisions:
        if not d.artifact_key.startswith(prefix):
            continue
        try:
            blob, _ = store.get_blob(d.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception:
            continue
        if not include_resolved and body.get("resolved_at"):
            continue
        out.append(body)
    # Sort so architectural decisions surface above value/policy ones —
    # they tend to block more downstream work. Then by recency.
    type_rank = {
        "architectural": 0, "contract": 1, "policy": 2, "value": 3,
    }
    out.sort(key=lambda b: (
        b.get("resolved_at") is not None,
        type_rank.get(b.get("decision_type", "policy"), 9),
        -(b.get("detected_at") or 0),
    ))
    return out


def resolve_decision_needed(
    store: LedgerStore, project_id: str, digest: str,
    *,
    action: str,
    value: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Record a user resolution.

    Two actions:
      - ``"provide_value"``: user supplied an answer. ``value`` must be
        non-empty. Returns the resolved-record dict so the caller can
        synthesize a Finding for re-injection into the next fix-all.
      - ``"dismiss"``: user decided the finding is fine as-is (e.g.,
        the placeholder URL is intentional for an internal tool).
        Returns None.
    """
    if action not in {"provide_value", "dismiss"}:
        raise ValueError(
            f"action must be 'provide_value' or 'dismiss', got {action!r}"
        )
    if action == "provide_value" and not (value and value.strip()):
        raise ValueError("provide_value requires a non-empty value")

    target_key = f"decision_needed:{project_id}:{digest}"
    current = store.current_entry(project_id, target_key)
    if current is None:
        return None
    try:
        blob, _ = store.get_blob(current.blob_sha256)
        body = json.loads(blob.decode("utf-8")) if blob else {}
    except Exception:
        return None

    body["resolved_at"] = time.time()
    body["resolved_action"] = action
    body["resolved_value"] = value.strip() if (action == "provide_value" and value) else None

    store.write_entry(
        project_id=project_id,
        tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=target_key,
        body=body,
        rationale=(
            f"User resolved decision on {body.get('file_path', '?')}: "
            + (f"provided value (len={len(value or '')})"
               if action == "provide_value" else "dismissed (leave as-is)")
        ),
        author="user:resolve_decision",
    )
    return body if action == "provide_value" else None


def loaded_resolved_decisions_as_findings(
    store: LedgerStore, project_id: str,
) -> list[Finding]:
    """Return Finding objects synthesized from user-resolved decisions
    with action=provide_value, capped so a resolution that's already
    been acted on by a later fix-all pass isn't re-queued.

    The synthesized Finding includes the user's value woven into the
    suggestion so the Builder knows what to write. Auditor is set to
    'user:decision' so disagreement detection doesn't pull it into a
    new group.
    """
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    prefix = f"decision_needed:{project_id}:"

    most_recent_report_at = 0.0
    for d in decisions:
        if (d.artifact_key.startswith("fix_all:")
                and d.artifact_key.endswith(":report")):
            try:
                blob, _ = store.get_blob(d.blob_sha256)
                body = json.loads(blob.decode("utf-8")) if blob else {}
                completed = float(body.get("completed_at") or 0)
                if completed > most_recent_report_at:
                    most_recent_report_at = completed
            except Exception:
                continue

    out: list[Finding] = []
    for d in decisions:
        if not d.artifact_key.startswith(prefix):
            continue
        try:
            blob, _ = store.get_blob(d.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception:
            continue
        if body.get("resolved_action") != "provide_value":
            continue
        resolved_at = float(body.get("resolved_at") or 0)
        if resolved_at <= most_recent_report_at:
            continue
        value = body.get("resolved_value") or ""
        if not value:
            continue
        out.append(Finding(
            file_path=body.get("file_path", ""),
            severity="warning",
            line=body.get("line"),
            issue=body.get("issue", ""),
            suggestion=(
                f"User-provided value: {value}. "
                f"Apply this exactly where the original finding pointed."
            ),
            auditor="user:decision",
        ))
    return out


def suppress_unfixable_from_regressions(
    post_findings: list[Finding],
    unfixable_records: list[dict[str, Any]],
    *,
    similarity_threshold: float = 0.45,
) -> tuple[list[Finding], list[Finding]]:
    """Drop from `post_findings` any finding that matches a Builder-
    flagged unfixable record. These aren't really regressions — the
    Builder explicitly declined to fix them and surfaced them as
    decisions-needed.

    Returns (filtered_post, suppressed). Matching is per-file + Jaccard
    overlap on issue text, same threshold as the oscillation filter.

    `unfixable_records` should be the union of pending decisions from
    this pass and previous passes (we don't want a previously-flagged
    refusal to bounce back into the regression count just because the
    Builder didn't re-emit it this pass).
    """
    if not unfixable_records:
        return list(post_findings), []
    by_file: dict[str, list[str]] = {}
    for r in unfixable_records:
        path = r.get("file_path") or ""
        issue = r.get("issue") or ""
        if path and issue:
            by_file.setdefault(path, []).append(issue)

    kept: list[Finding] = []
    suppressed: list[Finding] = []
    for f in post_findings:
        candidates = by_file.get(f.file_path, [])
        suppressed_here = False
        for prior_issue in candidates:
            if _findings_overlap_threshold(f.issue, prior_issue) >= similarity_threshold:
                suppressed_here = True
                break
        if suppressed_here:
            suppressed.append(f)
        else:
            kept.append(f)
    return kept, suppressed
