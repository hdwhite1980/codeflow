"""
codeflow.audit_pipeline
=======================

The first auditor layer of the AI council. Given files that Anthropic
generated, ask OpenAI to read them and produce structured findings.
Write the findings as `audit_verdict` ledger entries with edges back to
the files they audited.

What this does NOT do (yet)
---------------------------
* No loop — audits are recorded, never block, never trigger patches.
* No second auditor — we're starting with OpenAI alone.
* No synthesis — findings are surfaced as ledger entries for the UI to
  display, not merged or ranked.

The whole point of starting flat is to gather evidence. After running
this in production for a while, we'll know:
  - What kinds of issues OpenAI actually catches
  - How often findings are real vs nitpicks
  - Whether the cost is worth it
  - Whether to layer a second auditor or just iterate on the prompt

Output shape
------------
Each audit verdict is one ledger entry per file. The body is a JSON
object with a `findings` array, where each finding has:
  - severity: "critical" | "warning" | "nit"
  - category: "security" | "correctness" | "style" | "performance" | "other"
  - line: int or null (best-effort line number; OpenAI estimates)
  - issue: short prose description
  - suggested_fix: short prose suggestion, or null if no clear fix

If the model returns no issues, we still write an entry with
`findings: []`. That distinguishes "audited and clean" from "never
audited" — important for the eventual loop and for the UI.

Failure mode
------------
If OpenAI returns garbage JSON twice, we write a verdict entry with
`findings: []` and a `parse_error` flag in the body. The user sees that
the audit was attempted but failed; we don't pretend the file is clean.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from ledger import ArtifactKind, LedgerStore, Tier
from openai_client import OpenAIClient, OpenAIError
from gemini_client import GeminiClient, GeminiError
from usage_recorder import UsageRecorder


# Exceptions to treat as "this whole auditor is dead, stop calling" —
# 401/403 from any provider means the API key is bad or model access is
# revoked, and every subsequent call will fail the same way.
_AUTH_ERROR_TYPES = (OpenAIError, GeminiError)


# Max files to audit per project. Same cap as the build pipeline — if the
# build wrote N files, we audit at most N. (Defensive, in case the ledger
# returns more entries than expected.)
MAX_FILES_PER_AUDIT = 25

# Re-try once on parse failure, same policy as the spec parser.
MAX_AUDIT_RETRIES = 1


# ---------------------------------------------------------------------------
# Prompt construction.
# ---------------------------------------------------------------------------

AUDIT_SCHEMA_DESCRIPTION = """
Return a JSON object matching this shape exactly:

{
  "findings": [
    {
      "severity": "critical" | "warning" | "nit",
      "category": "security" | "correctness" | "style" | "performance" | "other",
      "line": <int or null>,
      "issue": "<one-sentence description of the issue>",
      "suggested_fix": "<one-sentence suggestion, or null if no clear fix>"
    }
  ]
}

Severity rules (be strict, not gentle):
- "critical" — the code is non-functional or actively dangerous:
    * Truncated mid-line, missing braces, won't parse / won't compile / won't import
    * Will crash on the first realistic input (NoneType error, index out of range, missing import)
    * Hard-coded secrets, SQL injection, command injection, path traversal,
      missing auth on an endpoint that mutates state
    * Eval/exec on user input
  When in doubt between critical and warning for non-functional code:
  CHOOSE CRITICAL. A reviewer who would block the merge picks critical.
- "warning" — the code works but has a real defect a reviewer would block on:
    * Resource exhaustion paths (unbounded loops, no file size limits, no rate limits)
    * Silent data corruption (lossy decoding, malformed input accepted)
    * Documentation contradicting implementation
    * Missing error handling on operations that can fail
    * Tests that assert too loosely to catch regressions
- "nit" — stylistic preference, taste, or minor improvement that wouldn't
  block a real PR review.

Category rules:
- "security": auth, authz, injection, secrets, file upload validation,
  resource exhaustion, missing rate limits, unbounded input.
- "correctness": logic bugs, edge cases, contract violations, doc-vs-code
  drift, missing error handling.
- "style": naming, formatting, idioms.
- "performance": slowness, waste, missing batching/caching.
- "other": anything else.

Output rules:
- If the file is genuinely clean, return {"findings": []}. Do not invent
  issues to look thorough.
- "line" is the 1-indexed line number of the issue if identifiable; null otherwise.
- Output JSON only — no prose, no markdown fences.
"""


AUDIT_SYSTEM_PROMPT = """\
You are a senior code reviewer auditing one file at a time. Your job is
to find the issues a reviewer would block a merge on. Be strict, not gentle.

You apply equal scrutiny to:
  * Correctness — does the code do what its declared purpose says?
    Does it match its own documentation? Will it work on realistic inputs?
  * Security — auth, authz, input validation, resource exhaustion,
    file size limits, MIME-type enforcement, injection paths, secrets,
    eval/exec on untrusted data.
  * Robustness — error handling, edge cases, malformed input, network
    failures, partial reads.
  * Test quality — for test files, assert specific behavior (status codes,
    response shapes). Tests that accept multiple status codes ("200 or 400")
    or that don't actually verify outcomes are themselves bugs.
  * Packaging — missing __init__.py, unpinned dependencies, missing entries
    in requirements files, broken imports between files.

If you see code that is truncated, unparseable, or won't run as written,
flag it as CRITICAL even if it "looks fine apart from being cut off".

You do not invent issues. A genuinely clean file gets an empty findings
array. But you also do not rubber-stamp — if there are real concerns,
list them. The goal is the same triage decision a careful human reviewer
would make.

Output JSON only.
"""


def _audit_user_prompt(*, file_path: str, file_content: str,
                       purpose: str, language: str) -> str:
    """Compose the prompt asking the auditor to review one file.

    We deliberately do NOT include the project spec or other files.
    The auditor should audit each file on its own merits — does this
    file do what its declared purpose says? — rather than rubber-
    stamping based on shared context with the generator."""
    return (
        f"File: {file_path}\n"
        f"Language: {language}\n"
        f"Declared purpose: {purpose}\n\n"
        f"--- FILE CONTENTS ---\n"
        f"{file_content}\n"
        f"--- END FILE ---\n\n"
        f"Audit this file. Work through each category in order:\n"
        f"  1. Is the file complete? (not truncated, parses, imports)\n"
        f"  2. Does it accomplish its declared purpose?\n"
        f"  3. Are there security issues? (auth, input validation, "
        f"resource limits, injection)\n"
        f"  4. Are there correctness bugs? (logic errors, edge cases, "
        f"doc-vs-code drift)\n"
        f"  5. Is error handling adequate?\n"
        f"  6. For test files: do tests assert specific behavior, or "
        f"do they accept too many outcomes?\n"
        f"  7. For config files (requirements.txt, package.json, etc.): "
        f"are versions pinned? Are dependencies appropriate?\n\n"
        f"{AUDIT_SCHEMA_DESCRIPTION}"
    )


# ---------------------------------------------------------------------------
# Parsing.
# ---------------------------------------------------------------------------

_JSON_OBJECT_PATTERN = re.compile(r"\{.*\}", re.DOTALL)

_VALID_SEVERITIES = {"critical", "warning", "nit"}
_VALID_CATEGORIES = {"security", "correctness", "style", "performance", "other"}


def _extract_json(text: str) -> Optional[dict[str, Any]]:
    """Pull a JSON object out of a response. Same forgiving pattern as
    build_pipeline._extract_json — most of the time the response is pure
    JSON (because we use json_mode), but in case fences or prose sneak in
    we tolerate that."""
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_PATTERN.search(text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _validate_findings(raw: Any) -> Optional[list[dict[str, Any]]]:
    """Validate the shape of `raw["findings"]` and return a sanitized
    list. Returns None if the shape is wrong. Individual malformed
    findings are dropped rather than failing the whole audit — partial
    audit data is more useful than no audit data."""
    if not isinstance(raw, dict):
        return None
    findings_raw = raw.get("findings")
    if not isinstance(findings_raw, list):
        return None
    if len(findings_raw) > 100:
        # 100 findings on one file means the model is hallucinating or
        # the file is enormous. Either way, truncate; preserve the first
        # 100 so the user sees what was caught.
        findings_raw = findings_raw[:100]

    out: list[dict[str, Any]] = []
    for f in findings_raw:
        if not isinstance(f, dict):
            continue
        severity = f.get("severity")
        category = f.get("category")
        if severity not in _VALID_SEVERITIES:
            continue
        if category not in _VALID_CATEGORIES:
            category = "other"
        line = f.get("line")
        if not (line is None or isinstance(line, int)):
            line = None
        issue = f.get("issue")
        if not isinstance(issue, str) or not issue.strip():
            continue
        suggested = f.get("suggested_fix")
        if suggested is not None and not isinstance(suggested, str):
            suggested = None
        out.append({
            "severity": severity,
            "category": category,
            "line": line,
            "issue": issue.strip(),
            "suggested_fix": suggested.strip() if isinstance(suggested, str) else None,
        })
    return out


# ---------------------------------------------------------------------------
# Audit verdict key helper.
# ---------------------------------------------------------------------------

def audit_verdict_key(project_id: str, file_path: str, auditor: str) -> str:
    """Stable key for a verdict. Includes the auditor so we can have
    multiple auditors per file in the future (gemini, openai, etc.)
    without conflict. Uses the `ref:` prefix because the ledger maps
    `ref:` to DECISION_RECORD — and audit verdicts are conceptually
    decision records about file quality. AUDIT_VERDICT kind would be
    cleaner, but `ref:` keeps us inside the existing prefix allowlist
    without a ledger change."""
    return f"ref:{project_id}:audit:{auditor}:{file_path}"


# ---------------------------------------------------------------------------
# Outcome dataclass.
# ---------------------------------------------------------------------------

@dataclass
class AuditOutcome:
    audited_files: list[str]
    failed_files: list[tuple[str, str]]    # (path, error message)
    total_findings: int
    critical_count: int
    warning_count: int
    nit_count: int
    total_input_tokens: int
    total_output_tokens: int

    @property
    def all_clean(self) -> bool:
        """True if every audit produced an empty findings array."""
        return self.total_findings == 0 and not self.failed_files


# ---------------------------------------------------------------------------
# Pipeline.
# ---------------------------------------------------------------------------

async def run_audit(
    *,
    project_id: str,
    file_artifacts: list[dict[str, Any]],
    client: Any,  # OpenAIClient | GeminiClient — share the .complete() shape
    store: LedgerStore,
    auditor_name: str = "openai",
    provider: str = "openai",
    recorder: Optional[UsageRecorder] = None,
) -> AuditOutcome:
    """Audit each generated file in the project.

    Parameters
    ----------
    file_artifacts : list of dicts with keys `path`, `content`, `purpose`,
        `language`. The caller (job_handlers.handle_audit_project) reads
        these from the ledger and shapes them.

    client : an OpenAIClient or GeminiClient. They share the same
        `complete()` interface (one method, same kwargs, returns text +
        tokens). We type as Any because Python doesn't have structural
        typing without a Protocol, and adding one for two clients with
        identical shape is overkill.

    auditor_name : free-form identifier baked into the verdict's
        artifact_key and author fields. Use "openai" or "gemini" today;
        could become "openai-strict" or "gemini-security-focused" if we
        specialize prompts per auditor in the future.

    provider : the value written into token_usage.provider. Separate from
        auditor_name because we might run two prompt variants through
        the same provider — both rows want "openai" in the provider
        column but different auditor names in the verdict key."""

    audited: list[str] = []
    failed: list[tuple[str, str]] = []
    total_in = 0
    total_out = 0
    critical = 0
    warning = 0
    nit = 0
    total_findings = 0

    print(f"[audit] run_audit starting: project={project_id} "
          f"files={len(file_artifacts)} auditor={auditor_name} provider={provider}",
          flush=True)

    for fa in file_artifacts[:MAX_FILES_PER_AUDIT]:
        path = fa["path"]
        content = fa["content"]
        purpose = fa.get("purpose", "(unknown)")
        language = fa.get("language", "(unknown)")
        try:
            findings, parse_failed, tin, tout = await _audit_one_file(
                file_path=path, file_content=content,
                purpose=purpose, language=language,
                client=client,
                project_id=project_id, auditor_name=auditor_name,
                provider=provider,
                recorder=recorder,
            )
            total_in += tin
            total_out += tout

            body = {
                "auditor": auditor_name,
                "file_path": path,
                "findings": findings,
            }
            if parse_failed:
                body["parse_error"] = True

            for f in findings:
                if f["severity"] == "critical":
                    critical += 1
                elif f["severity"] == "warning":
                    warning += 1
                elif f["severity"] == "nit":
                    nit += 1
            total_findings += len(findings)

            store.write_entry(
                project_id=project_id,
                tier=Tier.AUDIT,
                artifact_kind=ArtifactKind.AUDIT_VERDICT,
                artifact_key=audit_verdict_key(project_id, path, auditor_name),
                body=body,
                rationale=(
                    f"{auditor_name} audit of {path}: "
                    f"{len(findings)} findings"
                    + (" (parse error; see body)" if parse_failed else "")
                ),
                author=f"{auditor_name}:audit",
                # Audit verdicts are ledger-only; not graph nodes. The
                # file_path is recorded in the body so the UI can join
                # verdicts back to files without traversing the graph.
            )
            audited.append(path)
        except _AUTH_ERROR_TYPES as exc:
            # Provider-specific error from either OpenAI or Gemini.
            err_name = type(exc).__name__
            print(f"[audit] {err_name} on {path}: status={exc.status_code} "
                  f"body={exc.body[:200]!r}", flush=True)
            failed.append((path, f"{err_name} {exc.status_code}: {exc.body[:200]}"))
            if exc.status_code in (401, 403):
                # Auth error / model access revoked means every subsequent
                # call will fail the same way. Stop.
                print(f"[audit] auth error on {auditor_name}: aborting "
                      f"remaining audits", flush=True)
                break
        except Exception as exc:
            print(f"[audit] unexpected error on {path}: "
                  f"{type(exc).__name__}: {exc!r}", flush=True)
            failed.append((path, f"{type(exc).__name__}: {exc}"))

    print(f"[audit] run_audit done: auditor={auditor_name} "
          f"audited={len(audited)} failed={len(failed)} "
          f"total_findings={total_findings}",
          flush=True)

    return AuditOutcome(
        audited_files=audited,
        failed_files=failed,
        total_findings=total_findings,
        critical_count=critical,
        warning_count=warning,
        nit_count=nit,
        total_input_tokens=total_in,
        total_output_tokens=total_out,
    )


async def _audit_one_file(
    *, file_path: str, file_content: str, purpose: str, language: str,
    client: Any,
    project_id: str,
    auditor_name: str,
    provider: str,
    recorder: Optional[UsageRecorder],
) -> tuple[list[dict[str, Any]], bool, int, int]:
    """Run one audit. Returns (findings, parse_failed, tokens_in, tokens_out).

    Retries once on parse failure. If both attempts fail to parse,
    returns ([], True, ...) — empty findings with the parse_failed flag
    set so the caller writes a verdict noting the audit didn't produce
    usable output."""
    prompt = _audit_user_prompt(
        file_path=file_path, file_content=file_content,
        purpose=purpose, language=language,
    )
    tokens_in = 0
    tokens_out = 0

    for attempt in range(MAX_AUDIT_RETRIES + 1):
        result = await client.complete(
            prompt=prompt,
            system=AUDIT_SYSTEM_PROMPT,
            temperature=0.1,
            json_mode=True,
        )
        tokens_in += result.input_tokens
        tokens_out += result.output_tokens
        if recorder is not None:
            recorder.record(
                project_id=project_id, provider=provider,
                model=result.model, stage="audit",
                subject=file_path,
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
            )
        raw = _extract_json(result.text)
        if raw is not None:
            findings = _validate_findings(raw)
            if findings is not None:
                return findings, False, tokens_in, tokens_out
        # Re-ask with a remediation hint. Keep it short — we don't want
        # to balloon the prompt.
        prompt = (
            f"Your previous response was not valid JSON matching the "
            f"schema. Try again.\n\n{prompt}"
        )

    return [], True, tokens_in, tokens_out
