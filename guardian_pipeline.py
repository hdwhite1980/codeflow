"""
codeflow.guardian_pipeline
==========================

The guardian's read-time intelligence. NOT part of the build pipeline.
This module's job is to maintain a layer of semantic understanding on
top of the structural ledger we already have.

Three responsibilities (this module covers #1; later turns add #2-3):

  1. INDEXING: when a file lands in the ledger, produce a structured
     summary of what it means — what it does, what state it touches,
     what it assumes, what could go wrong. Persist as a SEMANTIC_SUMMARY
     ledger entry attached to the file.

  2. RISK ANALYSIS: when the user asks "what breaks if I change X?",
     combine structural impact (graph queries) with semantic context
     (these summaries) and produce a real risk assessment.

  3. AMBIENT REVIEW: watch new artifacts as they land. Flag semantic
     anomalies (a query missing the standard tenant_id filter; a new
     auth path that doesn't honor the existing retry policy).

Architecture
------------
Guardian writes are decoupled from the build pipeline. The build
pipeline produces FILE artifacts; the guardian reads them and writes
SEMANTIC_SUMMARY artifacts as a follow-on step. If the guardian is
down, builds still work — they just don't get indexed until the
guardian comes back up.

Why structured summaries instead of free-text
---------------------------------------------
A wall of paragraphs is hard to query. We persist a JSON document
with named fields (purpose, touches, assumes, failure_modes, risk_notes).
This lets the risk analyzer (next turn) compose summaries cheaply:
"give me the failure_modes from every file in the auth/ folder."

Output format
-------------
Each summary has two presentations:
  - `plain_english`: customer-facing, no jargon, 1-3 sentences
  - `technical`: engineer-facing, dense, references symbols and types

Plain comes first because that's what gets shown by default; technical
is one click away. The guardian generates BOTH in a single LLM call
because asking for one and then the other doubles the cost.

Model fallback
--------------
We default to qwen2.5-coder:14b but the client falls back to 7b/3b
if 14b isn't installed on the box. Summaries from smaller models are
shallower but still useful — the indexer doesn't fail, it just
produces lower-quality summaries.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from ledger import ArtifactKind, LedgerStore, Tier
from ollama_client import OllamaClient, OllamaError


# Skip indexing for files larger than this — they're rare, expensive to
# process, and usually generated boilerplate (lockfiles, vendored libs)
# where a semantic summary adds little. Configurable per-deployment if
# this becomes a real constraint.
MAX_INDEXABLE_BYTES = 50_000

# Skip files matching these path patterns. Generated, third-party, or
# binary content that semantic summaries don't help with.
SKIP_PATH_PATTERNS = [
    re.compile(r"\.(lock|min\.js|min\.css)$"),
    re.compile(r"^node_modules/"),
    re.compile(r"^\.git/"),
    re.compile(r"^dist/|^build/|^\.next/"),
    re.compile(r"package-lock\.json$"),
    re.compile(r"yarn\.lock$"),
    re.compile(r"poetry\.lock$"),
]


# ---------------------------------------------------------------------------
# Data shapes.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class FileSummary:
    """A guardian's understanding of one file.

    Fields are designed so the risk analyzer can pose queries like
    "files in this folder that touch user authentication state" without
    re-reading the source. The model is asked to populate every field;
    we tolerate missing ones gracefully but log when they're absent.
    """
    file_path: str
    plain_english: str           # 1-3 sentences, non-technical
    technical: str               # paragraph, engineer-facing
    purpose: str                 # what the file is for
    touches: list[str]           # state/entities it reads or writes
    assumes: list[str]           # invariants, preconditions
    failure_modes: list[str]     # how it can break
    risk_notes: list[str]        # things a reviewer would flag
    indexed_at: float            # epoch seconds; for staleness checks
    indexer_model: str           # which Ollama model produced this
    input_tokens: int
    output_tokens: int


# ---------------------------------------------------------------------------
# Prompt.
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM_PROMPT = """You are the guardian: a code-understanding AI that helps engineers reason about a codebase.

You receive one file at a time. For each file you produce a structured summary that helps the next engineer (or AI) understand the file without re-reading every line.

Your output must be STRICT JSON with this exact schema:

{
  "plain_english": "1-3 sentence summary that a non-engineer leader could understand",
  "technical": "1 paragraph technical summary referencing real function/class names and types",
  "purpose": "what this file is for, in one sentence",
  "touches": ["state, entities, services this file reads or writes"],
  "assumes": ["preconditions, invariants, environment expectations"],
  "failure_modes": ["realistic ways this code can break or misbehave"],
  "risk_notes": ["things a senior engineer would flag in code review"]
}

Rules:
- plain_english: NO jargon. No function names. Reads like an explanation to a non-coder.
- technical: USE jargon. Mention specific function/class names from the file.
- Each array field: 0-5 items, each a complete short sentence.
- Output ONLY the JSON object. No markdown fences, no commentary, no preamble.
"""


def _build_summary_prompt(
    file_path: str, content: str,
    language: str, imports: list[str],
) -> str:
    """Render the user prompt for a summarization call.

    We include imports because they're cheap context that helps the
    model understand what the file depends on. We do NOT include the
    full file content of imports — that's the risk analyzer's job."""
    imports_block = (
        "Imports: " + ", ".join(imports[:20])
        if imports else "Imports: (none extracted)"
    )
    return (
        f"File: {file_path}\n"
        f"Language: {language}\n"
        f"{imports_block}\n\n"
        f"--- FILE CONTENTS ---\n{content}\n--- END ---\n\n"
        "Produce the JSON summary."
    )


# ---------------------------------------------------------------------------
# Public entry points.
# ---------------------------------------------------------------------------

def should_index(file_path: str, content_bytes: int) -> tuple[bool, str]:
    """Decide whether a file is worth indexing.

    Returns (yes, reason). The reason is logged when we skip so the
    operator can verify the right files are being processed. We err
    on the side of indexing — false positives are cheap; false
    negatives mean missing knowledge."""
    if content_bytes > MAX_INDEXABLE_BYTES:
        return False, f"file too large ({content_bytes} bytes)"
    for pat in SKIP_PATH_PATTERNS:
        if pat.search(file_path):
            return False, f"path matches skip pattern: {pat.pattern}"
    return True, "ok"


async def summarize_file(
    *,
    file_path: str,
    content: str,
    language: str,
    imports: Optional[list[str]] = None,
    client: OllamaClient,
) -> FileSummary:
    """Produce a FileSummary for one file via Ollama.

    Raises OllamaError on transport failures. JSON parse failures fall
    back to a stub summary with the raw text in `technical` — better
    than no summary, and the caller can choose whether to persist or
    discard.
    """
    prompt = _build_summary_prompt(
        file_path=file_path, content=content,
        language=language, imports=imports or [],
    )
    # We deliberately set max_tokens generously. A complete structured
    # summary on a 300-line file runs ~400-700 output tokens; truncation
    # would corrupt the JSON and waste the call.
    result = await client.complete(
        prompt,
        system=_SUMMARY_SYSTEM_PROMPT,
        max_tokens=1500,
        temperature=0.1,  # we want consistency, not creativity
    )

    parsed = _parse_summary_json(result.text)
    return FileSummary(
        file_path=file_path,
        plain_english=str(parsed.get("plain_english", ""))[:1000],
        technical=str(parsed.get("technical", ""))[:3000],
        purpose=str(parsed.get("purpose", ""))[:500],
        touches=_coerce_string_list(parsed.get("touches"), max_items=10),
        assumes=_coerce_string_list(parsed.get("assumes"), max_items=10),
        failure_modes=_coerce_string_list(parsed.get("failure_modes"), max_items=10),
        risk_notes=_coerce_string_list(parsed.get("risk_notes"), max_items=10),
        indexed_at=time.time(),
        indexer_model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )


def write_file_summary(
    store: LedgerStore,
    project_id: str,
    summary: FileSummary,
) -> None:
    """Persist a FileSummary to the ledger as a SEMANTIC_SUMMARY entry.

    Artifact key: `semantic:<project_id>:<file_path>`. Subsequent indexes
    of the same file supersede the previous summary via the standard
    ledger versioning. Old summaries remain queryable via seq for audit
    trail purposes.
    """
    store.write_entry(
        project_id=project_id,
        # SPEC is the right tier here: SEMANTIC_SUMMARY describes what a
        # file *is*, akin to spec. GENERATION is for files that DO things.
        # AUDIT is for verdicts ABOUT things. SPEC fits cleanest.
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.SEMANTIC_SUMMARY,
        artifact_key=f"semantic:{project_id}:{summary.file_path}",
        body={
            "file_path": summary.file_path,
            "plain_english": summary.plain_english,
            "technical": summary.technical,
            "purpose": summary.purpose,
            "touches": summary.touches,
            "assumes": summary.assumes,
            "failure_modes": summary.failure_modes,
            "risk_notes": summary.risk_notes,
            "indexed_at": summary.indexed_at,
            "indexer_model": summary.indexer_model,
            "input_tokens": summary.input_tokens,
            "output_tokens": summary.output_tokens,
        },
        rationale=(
            f"Guardian indexed {summary.file_path} "
            f"({summary.indexer_model}): {summary.plain_english[:150]}"
        ),
        author="guardian:indexer",
    )


# ---------------------------------------------------------------------------
# Reader.
# ---------------------------------------------------------------------------

def load_file_summaries(
    store: LedgerStore, project_id: str,
) -> list[dict[str, Any]]:
    """Return all current SEMANTIC_SUMMARY entries for a project as a
    list of body dicts. Used by the risk analyzer (later turn) and the
    /guardian/summaries endpoint."""
    entries = store.all_current(project_id, ArtifactKind.SEMANTIC_SUMMARY)
    out: list[dict[str, Any]] = []
    for e in entries:
        # Only file-level summaries here; symbol-level entries use a
        # different key prefix and are handled separately.
        if not e.artifact_key.startswith("semantic:"):
            continue
        try:
            blob, _ = store.get_blob(e.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
            out.append(body)
        except Exception as exc:
            print(f"[guardian] failed to load summary {e.artifact_key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            continue
    return out


# ---------------------------------------------------------------------------
# Pipeline integration — formatting summaries as prompt context.
# ---------------------------------------------------------------------------

# Cap on how much guardian context we inject into any single prompt.
# Each summary line is ~150 chars (path + one-sentence purpose +
# one-line risk note). 25 summaries = ~4KB of context — meaningful
# without dominating the prompt. Bigger projects get truncated to the
# 25 most relevant; the "Project context" header makes the truncation
# visible to the model.
MAX_CONTEXT_SUMMARIES = 25


def build_pipeline_context(
    store: "LedgerStore",
    project_id: str,
    *,
    focus_paths: Optional[list[str]] = None,
    exclude_paths: Optional[list[str]] = None,
    max_summaries: int = MAX_CONTEXT_SUMMARIES,
) -> str:
    """Build a "Project context" block for inclusion in builder/auditor
    prompts. Returns the empty string if no summaries exist for the
    project — every caller checks for this and falls back to the
    pre-guardian behavior.

    Parameters
    ----------
    focus_paths
        Files most relevant to the current task (the file being built/
        audited, or files mentioned in the iteration plan). These get
        priority placement at the top.
    exclude_paths
        Files to leave out — typically the file currently being
        regenerated, since the prompt already contains its purpose
        and the summary would be stale by the end of the call.
    max_summaries
        Truncate to this many. Default 25 captures most projects
        whole; larger projects get the most-recently-indexed
        summaries first.

    Output shape (single string, ready to interpolate):

        Project context (from guardian semantic index):
          - app/main.py — FastAPI entry point. Touches: routing, middleware. Risk: should add /healthz endpoint.
          - app/db.py — Async SQLAlchemy connection pool. Assumes valid DATABASE_URL. Risk: manual disposal required.
          ...

    The format is intentionally terse. Each line is path + purpose +
    the most actionable risk note. Full summaries (with `failure_modes`,
    `assumes`, `touches`) live in the ledger and are accessible via
    risk-analyzer queries; the Builder/Auditor doesn't need that depth
    in every prompt.
    """
    summaries = load_file_summaries(store, project_id)
    if not summaries:
        # No guardian data for this project. Caller falls back to the
        # pre-guardian prompt format (bare file paths).
        return ""

    excluded = set(exclude_paths or [])
    relevant = [s for s in summaries if s.get("file_path") not in excluded]

    # Order: focus_paths first (in the order given), then everything
    # else by file_path. This way the Builder/Auditor sees the files
    # most relevant to its task at the top of the context block.
    if focus_paths:
        focus_set = set(focus_paths)
        focused = [s for s in relevant if s.get("file_path") in focus_set]
        # Preserve the order the caller gave us in focus_paths.
        focused.sort(key=lambda s: focus_paths.index(s["file_path"]))
        rest = sorted(
            [s for s in relevant if s.get("file_path") not in focus_set],
            key=lambda s: s.get("file_path", ""),
        )
        ordered = focused + rest
    else:
        ordered = sorted(relevant, key=lambda s: s.get("file_path", ""))

    ordered = ordered[:max_summaries]
    if not ordered:
        return ""

    lines = ["Project context (from guardian semantic index):"]
    for s in ordered:
        path = s.get("file_path", "?")
        purpose = (s.get("purpose") or "").strip()
        risks = s.get("risk_notes") or []
        # Pick the first risk note as the "headline" risk for this line.
        # The Builder/Auditor can ask for more via the risk endpoint
        # if it needs deeper context on a specific file.
        risk = risks[0] if risks else ""
        # Compact format. Truncation thresholds chosen so a 25-summary
        # block lands around 3-5KB.
        if risk:
            lines.append(
                f"  - {path} — {purpose[:160]} Risk: {risk[:120]}"
            )
        else:
            lines.append(f"  - {path} — {purpose[:200]}")

    # Trailing note so the Builder/Auditor knows the context is
    # background, not instruction.
    lines.append(
        "  (These are background context for cross-file reasoning. "
        "You are not editing or auditing these files unless explicitly told to.)"
    )
    return "\n".join(lines)


def build_pipeline_context_with_paths(
    store: "LedgerStore",
    project_id: str,
    *,
    focus_paths: Optional[list[str]] = None,
    exclude_paths: Optional[list[str]] = None,
    max_summaries: int = MAX_CONTEXT_SUMMARIES,
) -> tuple[str, list[str]]:
    """Same as build_pipeline_context, but also returns the list of
    file paths that were included in the context block.

    Used by the iteration pipeline (Turn G "memory visibility") so
    we can persist a memory_references record in the ledger — letting
    the frontend show "Guardian referenced 12 files: ..." per
    iteration. The string is suitable for prompt interpolation;
    the path list is for UI surfacing.

    Returns (context_string, referenced_paths). Both are empty when
    no summaries exist.
    """
    summaries = load_file_summaries(store, project_id)
    if not summaries:
        return "", []

    excluded = set(exclude_paths or [])
    relevant = [s for s in summaries if s.get("file_path") not in excluded]

    if focus_paths:
        focus_set = set(focus_paths)
        focused = [s for s in relevant if s.get("file_path") in focus_set]
        focused.sort(key=lambda s: focus_paths.index(s["file_path"]))
        rest = sorted(
            [s for s in relevant if s.get("file_path") not in focus_set],
            key=lambda s: s.get("file_path", ""),
        )
        ordered = focused + rest
    else:
        ordered = sorted(relevant, key=lambda s: s.get("file_path", ""))

    ordered = ordered[:max_summaries]
    if not ordered:
        return "", []

    lines = ["Project context (from guardian semantic index):"]
    referenced_paths: list[str] = []
    for s in ordered:
        path = s.get("file_path", "?")
        referenced_paths.append(path)
        purpose = (s.get("purpose") or "").strip()
        risks = s.get("risk_notes") or []
        risk = risks[0] if risks else ""
        if risk:
            lines.append(
                f"  - {path} — {purpose[:160]} Risk: {risk[:120]}"
            )
        else:
            lines.append(f"  - {path} — {purpose[:200]}")
    lines.append(
        "  (These are background context for cross-file reasoning. "
        "You are not editing or auditing these files unless explicitly told to.)"
    )
    return "\n".join(lines), referenced_paths


# ---------------------------------------------------------------------------
# Risk analyzer — the customer-visible "what breaks if I change X?" feature.
# ---------------------------------------------------------------------------
#
# This is the read-time intelligence that justifies the guardian to a
# customer. The structural graph alone can answer "12 callers depend on
# this column"; the risk analyzer combines that with semantic summaries
# of those callers to produce an actual risk assessment that mentions
# specific files and specific concerns.
#
# Pluggable model: defaults to local Ollama (privacy preserved), but
# accepts any client implementing .complete(prompt, *, system, max_tokens,
# temperature) — including AnthropicClient for customers who opt in to
# frontier-quality answers.


# Severity ladder used in risk assessments. Aligns with audit severities
# (critical/warning/nit) but adds "low" because risk queries cover a
# wider range than audit findings do — many changes are obviously safe.
_RISK_SEVERITIES = ["low", "medium", "high", "critical"]


@dataclass(frozen=True)
class RiskConcern:
    """One specific risk the analyzer flagged about a change.

    `path` is the file (or other artifact) where the concern lives.
    `reason` is one sentence explaining why this matters. Concerns are
    enumerated separately from the narratives so the frontend can render
    them as clickable chips that link back to specific files.
    """
    path: str
    reason: str
    severity: str  # one of _RISK_SEVERITIES


@dataclass(frozen=True)
class RiskAssessment:
    """The structured answer to a 'what breaks if I change X?' question.

    Has both plain_narrative (customer-facing, no jargon) and
    technical_narrative (engineer-facing, dense) per Hugh's spec for
    guardian outputs. The frontend renders plain by default with
    technical one click away.

    `confidence` reflects how confident the model says it is. Important
    because:
      - new projects have sparse graphs and shallow summaries
      - some questions are inherently underspecified
      - the model itself can be wrong
    Customers see a low-confidence answer and know to verify manually.
    """
    target: str
    change_description: str
    severity: str                    # overall: highest concern severity
    plain_narrative: str             # 2-3 sentences for non-engineers
    technical_narrative: str         # paragraph for engineers
    affected_paths: list[str]        # files in the structural impact set
    concerns: list[RiskConcern]      # specific risks, each tied to a path
    suggested_sequencing: list[str]  # ordered steps if applicable
    confidence: float                # 0.0-1.0
    analyzer_model: str              # which model produced this
    input_tokens: int
    output_tokens: int
    indexed_summary_count: int       # how many summaries informed this


_RISK_SYSTEM_PROMPT = """You are the guardian: a code-understanding AI that helps engineers reason about proposed changes to a codebase.

A user is proposing a change and asking what could break. You receive:
  - The target of the change (a file path, table name, function name, etc.)
  - A description of what they want to do
  - The structural impact set: every artifact that transitively depends on the target
  - Semantic summaries of those artifacts (what each file does, what it touches, what it assumes, what its failure modes are)

Your output must be STRICT JSON with this schema:

{
  "severity": "low|medium|high|critical",
  "plain_narrative": "2-3 sentences a non-engineer leader could understand",
  "technical_narrative": "1 paragraph with specifics — function names, types, sequencing concerns",
  "affected_paths": ["paths most likely to need changes"],
  "concerns": [
    {"path": "<path>", "reason": "<one sentence>", "severity": "low|medium|high|critical"}
  ],
  "suggested_sequencing": ["ordered steps to do this change safely, OR empty array if no specific order matters"],
  "confidence": 0.0
}

Rules:
- plain_narrative: NO jargon. NO function names. Reads like an explanation to a non-coder.
- technical_narrative: USE jargon. Mention specific function/class/column names from the summaries provided.
- concerns: 0-10 items. Each one's path MUST come from the affected_paths list or be the target itself.
- suggested_sequencing: include ONLY if there's a real ordering risk (migrations before code, schemas before queries, etc.). Otherwise empty array.
- confidence: a real number between 0.0 and 1.0. Lower it when the graph is sparse, the summaries are shallow, or the question is ambiguous.
- severity at the top is the SAME as the highest individual concern severity (or "low" if no concerns).
- Output ONLY the JSON object. No markdown fences, no commentary, no preamble.
"""


def _build_risk_prompt(
    *,
    target: str,
    change_description: str,
    impact_set: list[str],
    summaries: dict[str, dict[str, Any]],
) -> str:
    """Render the user prompt for a risk-analysis call.

    We give the model:
      1. The change in plain terms
      2. The structural impact list — paths that depend on the target
      3. Semantic summaries (purpose, touches, assumes, failure_modes,
         risk_notes) for each affected file we have summaries for

    Files in the impact set that we DON'T have summaries for are still
    listed — the model should note that its assessment is partial.
    """
    lines = [
        f"Target of the change: {target}",
        f"Change description: {change_description}",
        "",
        f"Structural impact set ({len(impact_set)} artifacts depend on the target):",
    ]
    if not impact_set:
        lines.append("  (none — nothing in the graph currently depends on this target)")
    else:
        for path in impact_set[:50]:  # cap so the prompt doesn't explode
            lines.append(f"  - {path}")
        if len(impact_set) > 50:
            lines.append(f"  ... and {len(impact_set) - 50} more (truncated)")
    lines.append("")
    lines.append("Semantic context for affected artifacts:")
    if not summaries:
        lines.append("  (no semantic summaries available — assessment will be"
                     " structural only; lower your confidence accordingly)")
    else:
        for path, summary in sorted(summaries.items()):
            purpose = summary.get("purpose", "")
            touches = summary.get("touches", []) or []
            assumes = summary.get("assumes", []) or []
            failure_modes = summary.get("failure_modes", []) or []
            risk_notes = summary.get("risk_notes", []) or []
            lines.append(f"\n  ## {path}")
            if purpose:
                lines.append(f"    Purpose: {purpose}")
            if touches:
                lines.append(f"    Touches: {'; '.join(touches[:6])}")
            if assumes:
                lines.append(f"    Assumes: {'; '.join(assumes[:6])}")
            if failure_modes:
                lines.append(f"    Failure modes: {'; '.join(failure_modes[:6])}")
            if risk_notes:
                lines.append(f"    Existing risk notes: {'; '.join(risk_notes[:4])}")
    lines.append("")
    lines.append("Produce the structured JSON risk assessment.")
    return "\n".join(lines)


async def analyze_change_risk(
    *,
    store: "LedgerStore",
    project_id: str,
    target: str,
    change_description: str,
    client: Any,  # OllamaClient or AnthropicClient — anything with .complete
) -> RiskAssessment:
    """Produce a structured risk assessment for a proposed change.

    `target` is the artifact_key being changed. For files this is
    "file:<project_id>:<path>"; the function also accepts a bare path
    and adds the prefix. For database columns it's "db_column:<table>.<col>".
    Any artifact_key the graph knows about works.

    `client` is the LLM client. Local Ollama is the default; Claude can
    be used for customers who opt in to frontier-quality answers via the
    GUARDIAN_RISK_MODEL config flag. We don't care which one — both
    implement the same .complete() interface.

    Raises if the model returns unparseable output or the LLM call fails.
    Callers should write a DECISION_RECORD ledger entry capturing both
    the question and the answer for audit-trail purposes.
    """
    # Normalize target: accept bare paths and add the file: prefix.
    if not target.startswith(("file:", "spec_entity:", "db_column:",
                              "db_table:", "spec_route:", "spec_contract:",
                              "function_symbol:", "feature:")):
        target_key = f"file:{project_id}:{target}"
    else:
        target_key = target

    # Step 1: structural impact walk.
    try:
        impact_keys = store.impact_set(project_id, target_key)
    except Exception as exc:
        # If the graph query fails, we can still produce an assessment
        # from the target's own summary. Log and continue.
        print(f"[guardian:risk] impact walk failed for {target_key}: "
              f"{type(exc).__name__}: {exc}", flush=True)
        impact_keys = set()

    # Convert artifact_keys back to readable paths for the prompt and
    # the response. For file artifacts the path is everything after
    # `file:<project_id>:`; for others we use the artifact_key as-is.
    impact_paths: list[str] = []
    for k in sorted(impact_keys):
        if k.startswith(f"file:{project_id}:"):
            impact_paths.append(k[len(f"file:{project_id}:"):])
        else:
            impact_paths.append(k)

    # Step 2: semantic context. We pull summaries for the target (if it's
    # a file) AND every file in the impact set. Non-file artifacts won't
    # have semantic summaries; that's expected.
    all_summaries = load_file_summaries(store, project_id)
    by_path: dict[str, dict[str, Any]] = {
        s.get("file_path", ""): s for s in all_summaries if s.get("file_path")
    }
    # Resolve target back to a path for summary lookup
    target_path = (
        target_key[len(f"file:{project_id}:"):]
        if target_key.startswith(f"file:{project_id}:")
        else target_key
    )

    relevant_summaries: dict[str, dict[str, Any]] = {}
    if target_path in by_path:
        relevant_summaries[target_path] = by_path[target_path]
    for p in impact_paths:
        if p in by_path:
            relevant_summaries[p] = by_path[p]

    # Step 3: prompt the model.
    prompt = _build_risk_prompt(
        target=target_path,
        change_description=change_description,
        impact_set=impact_paths,
        summaries=relevant_summaries,
    )
    result = await client.complete(
        prompt,
        system=_RISK_SYSTEM_PROMPT,
        max_tokens=2000,
        temperature=0.1,
    )

    parsed = _parse_summary_json(result.text)  # tolerant parser, same as file summaries
    concerns_raw = parsed.get("concerns") or []
    concerns: list[RiskConcern] = []
    for c in concerns_raw[:10]:
        if not isinstance(c, dict): continue
        sev = str(c.get("severity", "medium")).lower()
        if sev not in _RISK_SEVERITIES: sev = "medium"
        concerns.append(RiskConcern(
            path=str(c.get("path", ""))[:200],
            reason=str(c.get("reason", ""))[:500],
            severity=sev,
        ))

    severity = str(parsed.get("severity", "low")).lower()
    if severity not in _RISK_SEVERITIES: severity = "low"

    # Coerce confidence to a valid float in [0,1].
    try:
        confidence = float(parsed.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(1.0, confidence))

    return RiskAssessment(
        target=target_path,
        change_description=change_description,
        severity=severity,
        plain_narrative=str(parsed.get("plain_narrative", ""))[:1500],
        technical_narrative=str(parsed.get("technical_narrative", ""))[:4000],
        affected_paths=impact_paths,
        concerns=concerns,
        suggested_sequencing=_coerce_string_list(
            parsed.get("suggested_sequencing"), max_items=10,
        ),
        confidence=confidence,
        analyzer_model=result.model,
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
        indexed_summary_count=len(relevant_summaries),
    )


def write_risk_assessment(
    store: "LedgerStore",
    project_id: str,
    assessment: RiskAssessment,
    *,
    seq: int,
) -> None:
    """Persist a RiskAssessment as a DECISION_RECORD ledger entry.

    The audit trail records both the question (target + change_description)
    and the answer (severity + narratives + concerns + confidence). This
    is the compliance story for regulated customers: "the guardian
    flagged this on date X; the team proceeded anyway because Y."

    `seq` is the project-scoped risk query sequence number — each query
    gets a fresh integer so the audit history reads chronologically.
    """
    body = {
        "target": assessment.target,
        "change_description": assessment.change_description,
        "severity": assessment.severity,
        "plain_narrative": assessment.plain_narrative,
        "technical_narrative": assessment.technical_narrative,
        "affected_paths": assessment.affected_paths,
        "concerns": [
            {"path": c.path, "reason": c.reason, "severity": c.severity}
            for c in assessment.concerns
        ],
        "suggested_sequencing": assessment.suggested_sequencing,
        "confidence": assessment.confidence,
        "analyzer_model": assessment.analyzer_model,
        "input_tokens": assessment.input_tokens,
        "output_tokens": assessment.output_tokens,
        "indexed_summary_count": assessment.indexed_summary_count,
        "asked_at": time.time(),
    }
    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"guardian:risk:{seq}",
        body=body,
        rationale=(
            f"Guardian risk query #{seq}: "
            f"{assessment.target} — {assessment.change_description[:100]} "
            f"[severity={assessment.severity}, "
            f"confidence={assessment.confidence:.2f}]"
        ),
        author=f"guardian:risk:{assessment.analyzer_model}",
    )


def list_risk_assessments(
    store: "LedgerStore", project_id: str,
) -> list[dict[str, Any]]:
    """Return all guardian risk-query history for a project, newest first.

    Used by the frontend's risk-history panel — users can revisit prior
    questions and see how their understanding of risk evolved over time.
    """
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    out: list[dict[str, Any]] = []
    for d in decisions:
        if not d.artifact_key.startswith("guardian:risk:"):
            continue
        try:
            blob, _ = store.get_blob(d.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
            body["seq"] = int(d.artifact_key.split(":")[-1])
            out.append(body)
        except Exception as exc:
            print(f"[guardian:risk] failed to load {d.artifact_key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            continue
    # Newest first by `asked_at`; fall back to seq when missing.
    out.sort(key=lambda b: (b.get("asked_at", 0), b.get("seq", 0)), reverse=True)
    return out


def list_iteration_risks(
    store: "LedgerStore", project_id: str,
) -> dict[int, dict[str, Any]]:
    """Return pre/post risk records grouped by iteration seq.

    Result shape:
      {
        7: {"pre_risk": {...body...}, "post_risk": {...body...}, "cancelled": {...}},
        6: {"pre_risk": {...}},  # post may be missing if iteration failed
        ...
      }

    Used by the frontend's iteration history cards to render risk
    panels inline next to each iteration. Missing pre/post are simply
    absent from the inner dict, not None.
    """
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    by_seq: dict[int, dict[str, Any]] = {}
    for d in decisions:
        if not d.artifact_key.startswith("iteration:"):
            continue
        parts = d.artifact_key.split(":")
        # Expected shapes:
        #   iteration:<seq>:pre_risk
        #   iteration:<seq>:post_risk
        #   iteration:<seq>:cancelled
        if len(parts) < 3:
            continue
        try:
            seq = int(parts[1])
        except ValueError:
            continue
        phase = parts[2]
        if phase not in ("pre_risk", "post_risk", "cancelled"):
            continue
        try:
            blob, _ = store.get_blob(d.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception as exc:
            print(f"[guardian:risk] failed to load {d.artifact_key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            continue
        by_seq.setdefault(seq, {})[phase] = body
    return by_seq


def list_fix_all_risks(
    store: "LedgerStore", project_id: str,
) -> dict[int, dict[str, Any]]:
    """Same shape as list_iteration_risks but for fix_all:<seq>:* keys."""
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    by_seq: dict[int, dict[str, Any]] = {}
    for d in decisions:
        if not d.artifact_key.startswith("fix_all:"):
            continue
        parts = d.artifact_key.split(":")
        if len(parts) < 3:
            continue
        try:
            seq = int(parts[1])
        except ValueError:
            continue
        phase = parts[2]
        if phase not in ("pre_risk", "post_risk", "cancelled"):
            continue
        try:
            blob, _ = store.get_blob(d.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception as exc:
            print(f"[guardian:risk] failed to load {d.artifact_key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            continue
        by_seq.setdefault(seq, {})[phase] = body
    return by_seq


def list_memory_references(
    store: "LedgerStore", project_id: str,
) -> dict[int, dict[str, Any]]:
    """Return per-iteration memory reference records.

    Result shape:
      {7: {"referenced_paths": [...], "reference_count": 12, "context_chars": 3421},
       6: {...}}

    Used by the iteration history UI to show "Guardian referenced N
    files" badge with the file list one click away. Iterations that
    ran before guardian had any indexed summaries simply won't appear
    in the result (no reference record was written).
    """
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    by_seq: dict[int, dict[str, Any]] = {}
    for d in decisions:
        if not d.artifact_key.startswith("iteration:"):
            continue
        parts = d.artifact_key.split(":")
        if len(parts) < 3 or parts[2] != "memory_references":
            continue
        try:
            seq = int(parts[1])
        except ValueError:
            continue
        try:
            blob, _ = store.get_blob(d.blob_sha256)
            body = json.loads(blob.decode("utf-8")) if blob else {}
        except Exception as exc:
            print(f"[guardian:memory] failed to load {d.artifact_key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            continue
        by_seq[seq] = body
    return by_seq


def next_risk_seq(store: "LedgerStore", project_id: str) -> int:
    """Allocate the next risk query sequence number for a project.

    Counts existing guardian:risk:* decision records. Not strictly
    contention-safe — two simultaneous risk queries could allocate the
    same seq. In practice risk queries are user-initiated and serialized
    by the frontend, so this is fine. If it becomes a real problem we
    add a SELECT FOR UPDATE pattern.
    """
    decisions = store.all_current(project_id, ArtifactKind.DECISION_RECORD)
    existing = [
        int(d.artifact_key.split(":")[-1])
        for d in decisions
        if d.artifact_key.startswith("guardian:risk:")
        and d.artifact_key.split(":")[-1].isdigit()
    ]
    return (max(existing) + 1) if existing else 1


# ---------------------------------------------------------------------------
# Iteration-attached risk analysis — pre-flight and post-iteration.
# ---------------------------------------------------------------------------
#
# These wrap analyze_change_risk for the specific shape of risk queries
# that flow out of iteration and fix-all pipelines. The difference from
# the standalone risk analyzer:
#
#   - Multiple files are usually affected at once, not a single target.
#     We synthesize a virtual target string that lists them.
#   - The change_description comes from real pipeline data (the user's
#     iteration prompt, or the set of audit findings being fixed) rather
#     than from a free-text user query.
#   - The result is persisted with a key tied to the iteration/fix-all
#     seq, not a standalone risk seq, so the iteration card can render
#     it inline.


async def analyze_iteration_intent(
    *,
    store: "LedgerStore",
    project_id: str,
    iteration_prompt: str,
    planned_change_paths: list[str],
    planned_delete_paths: list[str],
    planned_new_paths: list[str],
    client: Any,
) -> RiskAssessment:
    """Pre-flight risk analysis for an iteration.

    Runs AFTER the planner has decided what to change but BEFORE the
    Builder regenerates anything. Lets the user (and the pipeline)
    see what could break before any code is generated.

    The target is a synthetic multi-file descriptor; the change
    description includes both the user's prompt and the planner's
    structural intent (changes/deletes/new files). The risk analyzer
    reasons over the impact set of each changed file and the
    semantic summaries of all involved files.

    For the impact walk we use the FIRST changed file as the target —
    this gives the analyzer a graph anchor. For projects where the
    iteration touches many files, the analyzer also receives the full
    list in the change_description so it knows what else is in play.
    """
    # Build a human-readable target. When only one file is changing,
    # use just that path (cleaner narrative). When multiple, summarize.
    all_paths = list(planned_change_paths) + list(planned_new_paths) + list(planned_delete_paths)
    if len(all_paths) == 1:
        target = all_paths[0]
    elif len(all_paths) == 0:
        # Empty plan — nothing to analyze. Return a minimal "no-op" assessment.
        return RiskAssessment(
            target="(empty plan)",
            change_description=iteration_prompt,
            severity="low",
            plain_narrative="The iteration plan is empty — nothing will change.",
            technical_narrative="No files were marked for change, creation, or deletion. The pipeline will skip regeneration.",
            affected_paths=[],
            concerns=[],
            suggested_sequencing=[],
            confidence=1.0,
            analyzer_model="(skipped)",
            input_tokens=0,
            output_tokens=0,
            indexed_summary_count=0,
        )
    else:
        target = all_paths[0]  # graph anchor; full list in change_description

    # Construct a rich change description so the analyzer sees both the
    # user's intent and the planner's structural decision.
    parts = [f"User iteration prompt: {iteration_prompt}"]
    if planned_change_paths:
        parts.append(f"Files to be regenerated: {', '.join(sorted(planned_change_paths))}")
    if planned_new_paths:
        parts.append(f"New files to be created: {', '.join(sorted(planned_new_paths))}")
    if planned_delete_paths:
        parts.append(f"Files to be deleted: {', '.join(sorted(planned_delete_paths))}")
    change_description = "\n".join(parts)

    return await analyze_change_risk(
        store=store,
        project_id=project_id,
        target=target,
        change_description=change_description,
        client=client,
    )


async def analyze_iteration_outcome(
    *,
    store: "LedgerStore",
    project_id: str,
    iteration_prompt: str,
    actual_changed_paths: list[str],
    actual_new_paths: list[str],
    actual_deleted_paths: list[str],
    client: Any,
) -> RiskAssessment:
    """Post-iteration risk analysis.

    Runs AFTER the Builder regenerates files and the audit completes.
    The input is the set of files that ACTUALLY changed (which may
    differ from what the planner predicted — the Builder sometimes
    touches files the planner didn't anticipate). The analyzer
    reasons over the impact of those real changes.

    Same response shape as analyze_iteration_intent. The narratives
    will be different in tone — past-tense, focused on what's now
    deployed rather than what's being proposed.
    """
    all_paths = list(actual_changed_paths) + list(actual_new_paths) + list(actual_deleted_paths)
    if not all_paths:
        return RiskAssessment(
            target="(no changes applied)",
            change_description=iteration_prompt,
            severity="low",
            plain_narrative="The iteration completed without applying any file changes.",
            technical_narrative="No files were regenerated, created, or deleted. Nothing to assess.",
            affected_paths=[],
            concerns=[],
            suggested_sequencing=[],
            confidence=1.0,
            analyzer_model="(skipped)",
            input_tokens=0,
            output_tokens=0,
            indexed_summary_count=0,
        )

    target = all_paths[0] if len(all_paths) >= 1 else "(no changes)"
    parts = [f"Iteration prompt was: {iteration_prompt}",
             "The iteration has just completed. Assess what may now be at risk."]
    if actual_changed_paths:
        parts.append(f"Files regenerated: {', '.join(sorted(actual_changed_paths))}")
    if actual_new_paths:
        parts.append(f"New files created: {', '.join(sorted(actual_new_paths))}")
    if actual_deleted_paths:
        parts.append(f"Files deleted: {', '.join(sorted(actual_deleted_paths))}")
    change_description = "\n".join(parts)

    return await analyze_change_risk(
        store=store,
        project_id=project_id,
        target=target,
        change_description=change_description,
        client=client,
    )


def write_iteration_risk(
    store: "LedgerStore",
    project_id: str,
    iteration_seq: int,
    phase: str,  # "pre_risk" or "post_risk"
    assessment: RiskAssessment,
) -> None:
    """Persist an iteration-attached risk assessment to the ledger.

    Key shape: `iteration:<seq>:pre_risk` or `iteration:<seq>:post_risk`.
    Stored as DECISION_RECORD so it shows up in the same query path as
    the iteration's other markers (plan, started, outcome).
    """
    if phase not in ("pre_risk", "post_risk"):
        raise ValueError(f"phase must be 'pre_risk' or 'post_risk', got {phase!r}")
    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"iteration:{iteration_seq}:{phase}",
        body={
            "target": assessment.target,
            "change_description": assessment.change_description,
            "severity": assessment.severity,
            "plain_narrative": assessment.plain_narrative,
            "technical_narrative": assessment.technical_narrative,
            "affected_paths": assessment.affected_paths,
            "concerns": [
                {"path": c.path, "reason": c.reason, "severity": c.severity}
                for c in assessment.concerns
            ],
            "suggested_sequencing": assessment.suggested_sequencing,
            "confidence": assessment.confidence,
            "analyzer_model": assessment.analyzer_model,
            "input_tokens": assessment.input_tokens,
            "output_tokens": assessment.output_tokens,
            "indexed_summary_count": assessment.indexed_summary_count,
            "asked_at": time.time(),
        },
        rationale=(
            f"Iteration #{iteration_seq} {phase.replace('_', ' ')}: "
            f"severity={assessment.severity}, "
            f"confidence={assessment.confidence:.2f}"
        ),
        author=f"guardian:iteration_{phase}:{assessment.analyzer_model}",
    )


def write_fix_all_risk(
    store: "LedgerStore",
    project_id: str,
    fix_all_seq: int,
    phase: str,  # "pre_risk" or "post_risk"
    assessment: RiskAssessment,
) -> None:
    """Same as write_iteration_risk but for fix-all passes.

    Key shape: `fix_all:<seq>:pre_risk` or `fix_all:<seq>:post_risk`.
    """
    if phase not in ("pre_risk", "post_risk"):
        raise ValueError(f"phase must be 'pre_risk' or 'post_risk', got {phase!r}")
    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.DECISION_RECORD,
        artifact_key=f"fix_all:{fix_all_seq}:{phase}",
        body={
            "target": assessment.target,
            "change_description": assessment.change_description,
            "severity": assessment.severity,
            "plain_narrative": assessment.plain_narrative,
            "technical_narrative": assessment.technical_narrative,
            "affected_paths": assessment.affected_paths,
            "concerns": [
                {"path": c.path, "reason": c.reason, "severity": c.severity}
                for c in assessment.concerns
            ],
            "suggested_sequencing": assessment.suggested_sequencing,
            "confidence": assessment.confidence,
            "analyzer_model": assessment.analyzer_model,
            "input_tokens": assessment.input_tokens,
            "output_tokens": assessment.output_tokens,
            "indexed_summary_count": assessment.indexed_summary_count,
            "asked_at": time.time(),
        },
        rationale=(
            f"Fix-all pass #{fix_all_seq} {phase.replace('_', ' ')}: "
            f"severity={assessment.severity}, "
            f"confidence={assessment.confidence:.2f}"
        ),
        author=f"guardian:fix_all_{phase}:{assessment.analyzer_model}",
    )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _parse_summary_json(raw_text: str) -> dict[str, Any]:
    """Strict JSON parsing with a fence-stripping fallback. Returns an
    empty dict on parse failure rather than raising — the caller will
    end up with a stub summary, which is better than no summary at all
    on a file that genuinely confused the model."""
    text = raw_text.strip()
    if text.startswith("```"):
        # Drop fence lines. Common shape: ```json\n{...}\n```
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1])
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {}
    except json.JSONDecodeError as exc:
        print(f"[guardian] summary JSON parse failed: {exc}; "
              f"first 300 chars: {raw_text[:300]!r}", flush=True)
        return {
            "plain_english": "(Guardian could not parse the model output for this file.)",
            "technical": raw_text[:2000],
            "purpose": "",
            "touches": [],
            "assumes": [],
            "failure_modes": [],
            "risk_notes": [],
        }


def _coerce_string_list(value: Any, max_items: int = 10) -> list[str]:
    """Make sure a field that should be list[str] actually is one.
    Models occasionally return a single string or a list of dicts.
    We accept whatever we got and try to extract strings."""
    if isinstance(value, list):
        out: list[str] = []
        for item in value[:max_items]:
            if isinstance(item, str):
                out.append(item[:500])
            elif isinstance(item, dict):
                # Try common keys.
                for key in ("text", "value", "description"):
                    if key in item and isinstance(item[key], str):
                        out.append(item[key][:500])
                        break
        return out
    if isinstance(value, str):
        return [value[:500]]
    return []
