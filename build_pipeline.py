"""
codeflow.build_pipeline
=======================

The first real generation pipeline. Given a project prompt, ask Anthropic
for a structured spec, then ask it to generate each file the spec calls
for. Every step writes ledger entries with edges back to what they
implement, so the impact graph is populated from the start.

This is single-AI mode
-----------------------
Only Anthropic, no voting, no audit gates yet. That's deliberate: get the
producer-consumer-ledger path proven end-to-end with one frontier model,
then layer additional models in as voting peers in a future iteration.
Each layer has its own failure modes; landing one at a time keeps
debugging tractable.

Why JSON-mode-via-prompt instead of structured output features
--------------------------------------------------------------
Anthropic exposes structured output via tool use and constrained generation
in some configurations. We're using "ask for JSON in the prompt, validate
on receipt" because:

  * It works identically with any frontier model — when we add OpenAI and
    Gemini as voting peers, the same prompts work everywhere.
  * Failures are easy to log and debug — you can see the broken JSON in
    the ledger and reason about what the model thought it was doing.
  * Strict-mode tool-use APIs change shape across providers and across
    Anthropic's own releases. The prompt-and-parse pattern has been
    stable for years.

Cost and safety limits
----------------------
Hard caps on:
  * MAX_FILES_PER_PROJECT — model could hallucinate "I need 800 files"
  * MAX_SPEC_RETRIES      — if the model returns invalid JSON, we ask
                            once more then give up. We don't retry-forever.
  * AnthropicClient.max_tokens — caps cost per call.

The build is best-effort. Partial success is acceptable: if 3 of 5 files
generate cleanly, we record those and log the rest as failures.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from anthropic_client import AnthropicClient, AnthropicError, CompletionResult
from ledger import ArtifactKind, EdgeKind, LedgerStore, Tier
from usage_recorder import UsageRecorder


# Provider tag baked into usage rows so the recorder can stay
# provider-agnostic. If we ever swap the build model from Anthropic to
# something else, this is the one knob to flip.
_PROVIDER = "anthropic"


# Hard ceiling so a confused model can't produce an unbounded run.
MAX_FILES_PER_PROJECT = 25

# How many times to re-ask if the spec response is not valid JSON.
# 1 means: try once, if parsing fails try one more time, then give up.
MAX_SPEC_RETRIES = 1


# ---------------------------------------------------------------------------
# Spec shape — what we ask the model to produce.
# ---------------------------------------------------------------------------

# We deliberately keep the shape small. Each entry must be enough to write a
# ledger entry from, plus enough context for the file-generation step to
# author the file. Anything more would inflate prompt size and cost.
SPEC_SCHEMA_DESCRIPTION = """
Return ONLY a valid JSON object matching this shape exactly:

{
  "summary": "<one-paragraph summary of what we're building>",
  "files": [
    {
      "path": "<relative file path, e.g. 'src/app.py' or 'README.md'>",
      "purpose": "<one-sentence purpose statement>",
      "language": "<primary language, e.g. 'python', 'typescript', 'markdown'>",
      "imports": ["<path of another file in this spec it depends on>", ...],
      "size_hint": "<small|medium|large — controls how detailed we ask for>"
    }
  ]
}

Rules:
- "files" must have at least 1 and at most 25 entries.
- Paths use forward slashes. No leading slash. No '..' segments.
- "imports" lists OTHER files in this same spec by their path. Empty list if none.
- Do not include any text outside the JSON object. No prose, no code fences, no commentary.
"""


SPEC_SYSTEM_PROMPT = """\
You are the planning step of an autonomous app builder. Given a user's
description of what they want, return a structured project plan as JSON.
Be concrete and pragmatic: list the actual files a working version of
this project needs, not aspirational scaffolding. Prefer fewer well-chosen
files over many small ones. Output JSON only, no commentary.
"""


FILE_SYSTEM_PROMPT_TEMPLATE = """\
You are the file-generation step of an autonomous app builder. The
overall project is described below. Your job is to write the COMPLETE
contents of one file. Output the raw file contents only — no commentary,
no markdown fences, no header explaining the file.

Project summary:
{summary}

File purpose:
{purpose}

File language:
{language}

Other files in the project (for context, do not generate these):
{other_files}
"""


# ---------------------------------------------------------------------------
# Spec datatypes.
# ---------------------------------------------------------------------------

@dataclass
class SpecFile:
    path: str
    purpose: str
    language: str
    imports: list[str]
    size_hint: str


@dataclass
class ProjectSpec:
    summary: str
    files: list[SpecFile]


# ---------------------------------------------------------------------------
# Parsing helpers.
# ---------------------------------------------------------------------------

# A modest pattern to find a JSON object anywhere in a response. Models
# occasionally wrap JSON in markdown despite instructions; this pulls the
# braces out regardless. Greedy match so nested braces survive.
_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    """Pull the first top-level JSON object out of a model response.

    Returns the parsed dict on success, None on failure. We do not raise
    here because the caller has a retry policy and the spec validator
    has more context than we do."""
    text = text.strip()
    # Cheap path: the response is already pure JSON.
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Fallback: maybe it's wrapped in fences or prose.
    match = _JSON_OBJECT_PATTERN.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _validate_path(path: str) -> bool:
    """Reject anything that looks like a path-escape attempt or
    obviously malformed input. We are not the sandbox — the sandbox
    enforces this for real — but stopping bad paths here avoids
    polluting the ledger with garbage."""
    if not path or path.startswith("/") or ".." in path.split("/"):
        return False
    if any(c in path for c in ("\\", "\x00", "\n", "\r")):
        return False
    return True


def _parse_spec(raw: dict[str, Any]) -> Optional[ProjectSpec]:
    """Validate the model's spec response and convert to ProjectSpec.

    Returns None on any validation failure. We log nothing here; the
    caller logs the raw response so we can debug what the model produced."""
    if not isinstance(raw, dict):
        return None
    summary = raw.get("summary")
    files_raw = raw.get("files")
    if not isinstance(summary, str) or not isinstance(files_raw, list):
        return None
    if not (1 <= len(files_raw) <= MAX_FILES_PER_PROJECT):
        return None

    files: list[SpecFile] = []
    seen_paths: set[str] = set()
    for entry in files_raw:
        if not isinstance(entry, dict):
            return None
        path = entry.get("path")
        purpose = entry.get("purpose")
        language = entry.get("language")
        imports = entry.get("imports", [])
        size_hint = entry.get("size_hint", "medium")
        if not isinstance(path, str) or not _validate_path(path):
            return None
        if path in seen_paths:
            return None  # Duplicates are almost always a model mistake.
        seen_paths.add(path)
        if not isinstance(purpose, str) or not purpose.strip():
            return None
        if not isinstance(language, str) or not language.strip():
            return None
        if not isinstance(imports, list) or not all(isinstance(i, str) for i in imports):
            return None
        if size_hint not in ("small", "medium", "large"):
            size_hint = "medium"
        files.append(SpecFile(
            path=path,
            purpose=purpose.strip(),
            language=language.strip(),
            imports=list(imports),
            size_hint=size_hint,
        ))

    return ProjectSpec(summary=summary.strip(), files=files)


# ---------------------------------------------------------------------------
# Ledger key helpers. Stable across runs so re-generation supersedes
# rather than producing parallel artifacts.
#
# The ledger validates artifact_key prefixes against a fixed allowlist
# (`_KEY_PREFIX_TO_NODE_KIND` in ledger.py). We use:
#   - "manifest:..."  for the top-level project manifest entry
#   - "entity:..."    for each planned file's spec entry (a SPEC_ENTITY)
#   - "file:..."      for the actual generated file content
#   - "ref:..."       for the bookend decision_records (started, outcome,
#                     skipped). The ledger maps "ref" to DECISION_RECORD.
# ---------------------------------------------------------------------------

def spec_manifest_key(project_id: str) -> str:
    return f"manifest:{project_id}:spec"


def spec_file_item_key(project_id: str, path: str) -> str:
    return f"entity:{project_id}:{path}"


def file_artifact_key(project_id: str, path: str) -> str:
    return f"file:{project_id}:{path}"


# ---------------------------------------------------------------------------
# Pipeline.
# ---------------------------------------------------------------------------

@dataclass
class BuildOutcome:
    """What the pipeline produced. Used by the handler to write a
    summary entry and by tests to assert on behavior."""
    spec: Optional[ProjectSpec]
    files_written: list[str]
    files_failed: list[tuple[str, str]]   # (path, error_message)
    total_input_tokens: int
    total_output_tokens: int

    @property
    def succeeded(self) -> bool:
        return self.spec is not None and not self.files_failed


async def run_build(
    *,
    project_id: str,
    prompt: str,
    client: AnthropicClient,
    store: LedgerStore,
    recorder: Optional[UsageRecorder] = None,
) -> BuildOutcome:
    """Execute the build pipeline for one project.

    Steps
    -----
    1. Ask Anthropic for a project spec (JSON). Retry once on parse failure.
    2. Validate the spec; abort if still invalid.
    3. Write the spec to the ledger as one SPEC_MANIFEST_ITEM and one
       SPEC_MANIFEST_ITEM per file (with declared dependencies as edges).
    4. For each file in the spec, ask Anthropic for the contents and
       write a FILE artifact with an implements-edge back to the spec.

    All ledger writes happen one at a time. We don't batch because the
    ledger's transactional unit is the entry — interleaving is a feature
    (the frontend can show progress as it happens via realtime).

    The optional `recorder` writes one row per API call to the
    token_usage table. Passing None disables recording — the pipeline
    still works, you just lose the per-call audit trail."""

    # Step 1+2: spec.
    spec, spec_tokens_in, spec_tokens_out = await _generate_spec(
        prompt, client, project_id=project_id, recorder=recorder,
    )
    if spec is None:
        return BuildOutcome(
            spec=None,
            files_written=[],
            files_failed=[],
            total_input_tokens=spec_tokens_in,
            total_output_tokens=spec_tokens_out,
        )

    # Step 3: write the spec manifest to the ledger.
    manifest_key = spec_manifest_key(project_id)
    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.SPEC_MANIFEST_ITEM,
        artifact_key=manifest_key,
        body={"summary": spec.summary, "file_count": len(spec.files)},
        rationale=f"Generated project spec via Anthropic. "
                  f"{len(spec.files)} files planned.",
        author="anthropic:spec",
    )

    # Per-file spec entries with declared dependency edges. We use
    # SPEC_ENTITY here, not SPEC_MANIFEST_ITEM — each planned file is an
    # entity in the spec graph, and the prefix-to-kind mapping in the
    # ledger expects "entity:" keys to be SPEC_ENTITY nodes. Using the
    # manifest item kind would silently disagree with the placeholder
    # nodes the ledger creates for forward-referenced edges.
    for f in spec.files:
        depends_on_keys = [
            spec_file_item_key(project_id, dep) for dep in f.imports
            # Only emit an edge if the import target was actually listed in
            # the spec; if the model invented an import target, we'd dangle
            # an edge to a node that never exists. Drop silently.
            if any(other.path == dep for other in spec.files)
        ]
        store.write_entry(
            project_id=project_id,
            tier=Tier.SPEC,
            artifact_kind=ArtifactKind.SPEC_ENTITY,
            artifact_key=spec_file_item_key(project_id, f.path),
            body={
                "path": f.path,
                "purpose": f.purpose,
                "language": f.language,
                "imports": f.imports,
                "size_hint": f.size_hint,
            },
            rationale=f"Planned file {f.path}: {f.purpose}",
            author="anthropic:spec",
            depends_on=depends_on_keys,
            satisfies_manifest_item=[manifest_key],
        )

    # Step 4: generate each file.
    written: list[str] = []
    failed: list[tuple[str, str]] = []
    total_in = spec_tokens_in
    total_out = spec_tokens_out

    for f in spec.files:
        try:
            content, tin, tout = await _generate_file(
                spec=spec, target=f, client=client,
                project_id=project_id, recorder=recorder,
            )
            total_in += tin
            total_out += tout
            store.write_entry(
                project_id=project_id,
                tier=Tier.GENERATION,
                artifact_kind=ArtifactKind.FILE,
                artifact_key=file_artifact_key(project_id, f.path),
                body=content,
                rationale=f"Generated {f.path} via Anthropic from spec.",
                author="anthropic:file",
                implements=[spec_file_item_key(project_id, f.path)],
            )
            written.append(f.path)
        except AnthropicError as exc:
            # Cost-control: an API failure on one file shouldn't tank the
            # whole build. Record and continue. Auth errors are a separate
            # category — those should abort the whole build because every
            # subsequent call will fail the same way.
            failed.append((f.path, f"AnthropicError {exc.status_code}: {exc.body[:200]}"))
            if exc.status_code in (401, 403):
                break
        except Exception as exc:
            # Any other exception (network, validation, etc.). Same policy:
            # record, continue. We do NOT swallow these silently — the
            # outcome carries the list of failures up to the handler.
            failed.append((f.path, f"{type(exc).__name__}: {exc}"))

    return BuildOutcome(
        spec=spec,
        files_written=written,
        files_failed=failed,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
    )


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------

async def _generate_spec(
    prompt: str, client: AnthropicClient,
    *,
    project_id: str,
    recorder: Optional[UsageRecorder],
) -> tuple[Optional[ProjectSpec], int, int]:
    """Ask the model for a spec, with a single retry on parse failure.

    Returns (spec or None, total_input_tokens, total_output_tokens).
    Token counters always reflect actual API usage so callers can record
    cost even on failure."""
    user_prompt = (
        f"User's project description:\n\n{prompt}\n\n"
        f"Produce the spec JSON as described.\n\n{SPEC_SCHEMA_DESCRIPTION}"
    )

    tokens_in = 0
    tokens_out = 0
    last_text = ""

    for attempt in range(MAX_SPEC_RETRIES + 1):
        result: CompletionResult = await client.complete(
            prompt=user_prompt,
            system=SPEC_SYSTEM_PROMPT,
            # Lower temperature for spec — we want structure, not creativity.
            temperature=0.1,
        )
        # Record this API call's tokens. Every retry counts as its own
        # call because the API charges us for each round trip; if the
        # first attempt returned garbage, we still paid for it.
        if recorder is not None:
            recorder.record(
                project_id=project_id, provider=_PROVIDER,
                model=result.model, stage="spec",
                subject=None,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        tokens_in += result.input_tokens
        tokens_out += result.output_tokens
        last_text = result.text
        raw = _extract_json(result.text)
        if raw is None:
            # Re-ask once with a remediation hint, then give up.
            user_prompt = (
                f"Your previous response was not valid JSON:\n\n"
                f"{result.text[:500]}\n\n"
                f"Return ONLY a valid JSON object matching the spec schema. "
                f"No prose, no fences.\n\n{SPEC_SCHEMA_DESCRIPTION}"
            )
            continue
        spec = _parse_spec(raw)
        if spec is not None:
            return spec, tokens_in, tokens_out
        # Valid JSON but failed schema validation. Same remediation path.
        user_prompt = (
            f"Your previous response was JSON but did not match the schema. "
            f"Try again, paying close attention to the rules.\n\n"
            f"{SPEC_SCHEMA_DESCRIPTION}"
        )

    print(f"[build] spec generation failed after retries; last response: "
          f"{last_text[:400]!r}")
    return None, tokens_in, tokens_out


# Output token caps tuned to file size hint. Small files don't need 4k tokens
# and capping them tightly keeps costs proportional to expected output.
_FILE_MAX_TOKENS = {
    "small":  1024,
    "medium": 2048,
    "large":  4096,
}


async def _generate_file(
    *, spec: ProjectSpec, target: SpecFile, client: AnthropicClient,
    project_id: str,
    recorder: Optional[UsageRecorder],
) -> tuple[str, int, int]:
    """Ask Anthropic for the contents of one file.

    Returns (text, input_tokens, output_tokens). Raises AnthropicError on
    API failure (caller decides how to handle). Records one usage row
    via `recorder` if provided."""

    # Tell the model what else is in the project, but don't include the
    # full file list every time — that bloats prompts. Just paths + purposes,
    # which is enough context to write a coherent file.
    other_files = "\n".join(
        f"  - {f.path}: {f.purpose}" for f in spec.files if f.path != target.path
    ) or "  (this is the only file)"

    system = FILE_SYSTEM_PROMPT_TEMPLATE.format(
        summary=spec.summary,
        purpose=target.purpose,
        language=target.language,
        other_files=other_files,
    )
    user_prompt = (
        f"Write the complete contents of `{target.path}`. "
        f"Output raw file contents only — no fences, no commentary."
    )

    max_tokens = _FILE_MAX_TOKENS.get(target.size_hint, 2048)
    result = await client.complete(
        prompt=user_prompt,
        system=system,
        # Slightly higher temperature for file authoring than spec — we want
        # natural code, not stiff template output.
        temperature=0.3,
        max_tokens=max_tokens,
    )
    if recorder is not None:
        recorder.record(
            project_id=project_id, provider=_PROVIDER,
            model=result.model, stage="file",
            subject=target.path,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
        )
    text = _strip_code_fences(result.text)
    return text, result.input_tokens, result.output_tokens


_FENCE_PATTERN = re.compile(
    r"^\s*```[^\n]*\n(.*?)\n```\s*$",
    re.DOTALL,
)


def _strip_code_fences(text: str) -> str:
    """Remove a single outer ``` fence if present. The prompt asks not
    to add them, but models occasionally include them anyway. We tolerate
    that one common case rather than failing the build over formatting."""
    match = _FENCE_PATTERN.match(text)
    if match:
        return match.group(1)
    return text
