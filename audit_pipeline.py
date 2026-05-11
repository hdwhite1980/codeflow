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
from usage_recorder import UsageRecorder


# Provider tag for the usage recorder. If we ever swap the auditor model
# to a different vendor, this is the knob to flip.
_PROVIDER = "openai"


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

Rules:
- If the file is clean, return {"findings": []}. Do not invent issues.
- "critical": code is broken, insecure, or will fail at runtime.
- "warning": code works but has a real problem (bug-prone, slow, fragile).
- "nit": stylistic preference, taste, or minor improvement.
- Categories: security (auth, injection, secrets, etc), correctness
  (logic bugs, edge cases), style (naming, formatting, idioms),
  performance (slowness, waste), other.
- "line" is the line number of the issue if you can identify one; null otherwise.
- Output JSON only — no prose, no markdown fences.
"""


AUDIT_SYSTEM_PROMPT = """\
You are a senior code reviewer auditing one file at a time. You are
strict about correctness and security, pragmatic about style. You do
not invent issues. If the file is fine, you say so by returning an
empty findings array. Output JSON only.
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
        f"Audit this file. Does it accomplish its declared purpose? "
        f"Are there bugs, security issues, or improvements worth flagging?\n\n"
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
    client: OpenAIClient,
    store: LedgerStore,
    auditor_name: str = "openai",
    recorder: Optional[UsageRecorder] = None,
) -> AuditOutcome:
    """Audit each generated file in the project.

    Parameters
    ----------
    file_artifacts : list of dicts with keys `path`, `content`, `purpose`,
        `language`. The caller (job_handlers.handle_audit_project) reads
        these from the ledger and shapes them.

    auditor_name : free-form identifier baked into the verdict's
        artifact_key and author fields. Lets us add gemini/o3/etc later
        without colliding."""

    audited: list[str] = []
    failed: list[tuple[str, str]] = []
    total_in = 0
    total_out = 0
    critical = 0
    warning = 0
    nit = 0
    total_findings = 0

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
        except OpenAIError as exc:
            failed.append((path, f"OpenAIError {exc.status_code}: {exc.body[:200]}"))
            if exc.status_code in (401, 403):
                # Auth error means every subsequent call will fail. Stop.
                break
        except Exception as exc:
            failed.append((path, f"{type(exc).__name__}: {exc}"))

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
    client: OpenAIClient,
    project_id: str,
    auditor_name: str,
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
                project_id=project_id, provider=_PROVIDER,
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
