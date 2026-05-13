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
