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


def next_fix_all_seq(store: LedgerStore, project_id: str) -> int:
    """Compute the next fix_all sequence number from existing markers."""
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    started = sum(
        1 for d in decisions
        if d.artifact_key.startswith("fix_all:")
        and d.artifact_key.endswith(":started")
    )
    return started + 1
