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
    lang_block = _language_audit_block(language, file_path)
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
        f"are versions pinned? Are dependencies appropriate?\n"
        f"{lang_block}"
        f"\n{AUDIT_SCHEMA_DESCRIPTION}"
    )


# ---------------------------------------------------------------------------
# Per-language audit concerns (Turn G-language).
# ---------------------------------------------------------------------------
#
# Generic auditor finds the obvious stuff. Each language has idiom-
# specific risks the generic prompt misses unless we name them.
#
# These blocks slot into the user prompt right after the category
# checklist. They're concrete enough that the auditor knows exactly
# what to look for, without being so prescriptive that it generates
# false positives on clean files.
#
# Notes on what each block targets:
#   - PowerShell: enterprise MSP audit material. Conditional Access,
#     Graph, Exchange, Intune scripts all share these failure modes.
#   - bash: defensive script hygiene. set -euo pipefail is table stakes
#     in any half-serious bash project.
#   - KQL: tenant isolation + perf. Sentinel/Defender queries that lack
#     tenant scoping are a critical security finding (data leak across
#     customers in MSSP environments).
#   - AppleScript: macOS-specific concerns around permission prompts and
#     System Events fragility.

def _language_audit_block(language: str, file_path: str) -> str:
    """Return language-specific audit guidance to append to the prompt.

    Returns an empty string for languages without specific concerns.
    The block is freeform prose appended to the audit checklist;
    severity recommendations are explicit so the auditor doesn't
    promote idiomatic-style preferences to critical.
    """
    lang = language.lower()
    if lang == "powershell":
        return _POWERSHELL_AUDIT_BLOCK
    if lang == "bash":
        return _BASH_AUDIT_BLOCK
    if lang == "kql":
        return _KQL_AUDIT_BLOCK
    if lang == "applescript":
        return _APPLESCRIPT_AUDIT_BLOCK
    return ""


_POWERSHELL_AUDIT_BLOCK = """
PowerShell-specific concerns (apply these in addition to the general checklist):

  Module / manifest hygiene
  -------------------------
  * If this is a *.psm1 module, does it have Export-ModuleMember calls
    or rely on the *.psd1 FunctionsToExport list? Implicit-export modules
    leak helper functions into the caller's session — warning at minimum.
  * If this is a *.psd1 manifest, does FunctionsToExport list specific
    function names? Using `*` is a security risk because it exports
    everything including helpers — warning.

  Cmdlet quality
  --------------
  * Public functions should declare [CmdletBinding()] so they support
    common parameters (-Verbose, -ErrorAction, -WhatIf when applicable).
    Missing [CmdletBinding()] on a public-looking function — warning.
  * Required parameters should be [Parameter(Mandatory)]. A function
    that silently does nothing when called with no arguments is a
    correctness bug — warning.
  * Parameters that accept pipelined input must declare ValueFromPipeline
    or ValueFromPipelineByPropertyName explicitly. Otherwise pipeline
    invocation silently works on `$null` — warning if the function
    looks pipeline-friendly.

  Error handling
  --------------
  * Use Try/Catch/Finally around any call that can fail (Invoke-RestMethod,
    Connect-MgGraph, file I/O, Remove-* cmdlets). Bare error-prone calls
    without handling — warning.
  * $ErrorActionPreference defaulting to 'Continue' lets non-terminating
    errors propagate silently. Long scripts should set
    `$ErrorActionPreference = 'Stop'` near the top or pass `-ErrorAction
    Stop` on critical calls — warning if absent.
  * Catch blocks that swallow $_ without logging or re-throwing —
    warning. The user loses the failure reason.

  Credentials and secrets
  -----------------------
  * Hard-coded credentials, API keys, tenant IDs, or client secrets in
    the script body — CRITICAL.
  * Plaintext credential parameters: a password parameter typed as
    [string] instead of [SecureString] or [PSCredential] — critical.
  * Use of `ConvertTo-SecureString -AsPlainText` for anything other
    than test fixtures — warning.

  Microsoft 365 / Graph specifics
  --------------------------------
  * Connect-MgGraph without explicit -Scopes argument — warning. The
    script inherits whatever scopes the user previously consented to,
    which is unpredictable and often over-privileged.
  * Get-* / Invoke-MgGraph* calls without paging support for large
    tenants (no -All, no -Top with handling, no @odata.nextLink loop) —
    warning. The script silently returns the first page only.
  * Disconnect-MgGraph missing at end of script — nit unless the script
    is long-lived.

  Output and pipeline correctness
  --------------------------------
  * Write-Host instead of Write-Output / Write-Information for non-UI
    output — nit, but warning if it breaks pipeline composition.
  * Functions returning $null implicitly by missing a return path —
    warning.
"""


_BASH_AUDIT_BLOCK = """
bash / shell-specific concerns (apply these in addition to the general checklist):

  Script hygiene
  --------------
  * Missing shebang line at the top of an executable script — warning
    (could be intentional for sourced libraries, in which case nit).
  * Missing `set -euo pipefail` (or equivalent: `set -e`, `set -u`,
    `set -o pipefail`) — warning. Scripts without these silently
    continue past failed commands and accumulate broken state.

  Variable expansion safety
  --------------------------
  * Unquoted `$variable` expansions in command arguments — warning.
    `rm $files` where $files contains spaces or globs is a real bug;
    `rm "$files"` is correct.
  * `$@` instead of `"$@"` when forwarding arguments — warning.
  * Unquoted command substitutions like `for f in $(ls)` — warning.
    Should be `for f in *` or `while read -r f; do ... done < <(find ...)`.

  Dangerous patterns
  ------------------
  * `rm -rf` with a variable in the path: `rm -rf "$DIR/*"` — CRITICAL
    if $DIR could be empty (would `rm -rf /*`). The safe form sets
    `: "${DIR:?must be set}"` first.
  * `eval` on untrusted input — CRITICAL.
  * Pipes to `bash` or `sh` from network sources (`curl | bash`) — warning
    for build scripts, critical for anything user-facing.
  * `cd "$dir"` without checking the cd succeeded — warning if subsequent
    commands assume the cwd. Use `cd "$dir" || exit` or `pushd`/`popd`.

  Argument parsing
  ----------------
  * Positional arguments only, no `getopts` or `--option` handling, for
    scripts taking more than 2 inputs — warning. Hard to maintain and
    error-prone.

  Portability
  -----------
  * `bash`-specific syntax (arrays, `[[`, `<<<`) in a script with `#!/bin/sh`
    shebang — warning. Either change the shebang to bash or use POSIX sh.
"""


_KQL_AUDIT_BLOCK = """
KQL-specific concerns (apply these in addition to the general checklist):

  Tenant scoping (CRITICAL for multi-tenant workspaces)
  ------------------------------------------------------
  * Sentinel / Log Analytics queries without tenant scoping in MSSP
    environments — CRITICAL. The query must include `| where TenantId
    == "<expected>"` or equivalent. Missing tenant scoping in a multi-
    tenant Log Analytics workspace leaks data across customers.
  * Hard-coded GUIDs for TenantId, SubscriptionId, etc. — warning. Use
    `let` bindings at the top so a reviewer can find them all in one
    place.

  Query performance
  -----------------
  * `where` filters placed after `join` instead of before — warning.
    Filters should run as early as possible to reduce scan volume.
  * Missing time-range filter on tables with months of data
    (SigninLogs, AuditLogs, SecurityEvent, etc.) — warning. Queries
    without `ago(<window>)` scan everything; expensive and slow.
  * `project` after `summarize` instead of before — warning. Projecting
    before summarize reduces the intermediate set size.
  * `union *` or wildcard table names in production queries — warning.
    Expensive and brittle to schema changes.

  Detection / hunting correctness
  --------------------------------
  * Detection queries that use `where AlertSeverity == "High"` without
    also filtering on the date/time or correlating to a workspace —
    warning. Returns historical alerts.
  * Hunting queries that return raw rows without aggregation —
    warning. Better as `summarize count() by ...` to surface patterns.
  * Use of `regex` matches without anchors (^ or $) on user-controlled
    fields — warning, can be exploited by attackers to evade detection
    via padded input.

  Output shape
  ------------
  * Final `project` or `summarize` step that drops fields needed by the
    downstream workbook / alert template — correctness warning.
  * Output column names that aren't snake_case or PascalCase consistent
    with the workspace convention — nit.

  Function-level
  --------------
  * `let` bindings used only once — nit (unless they document intent).
  * `let` bindings shadowing built-in functions or column names — warning.
"""


_APPLESCRIPT_AUDIT_BLOCK = """
AppleScript-specific concerns (apply these in addition to the general checklist):

  Error handling
  --------------
  * Operations that interact with other apps (`tell application` blocks)
    should be wrapped in `try` / `on error` — warning. Apps quit, refuse
    AppleEvents, or change their dictionaries between OS versions;
    bare blocks crash the whole script.
  * `on error` handlers that swallow the error message without logging
    — warning. The user has no idea what went wrong.

  Permission requirements
  -----------------------
  * Scripts using `tell application "System Events"` need Accessibility
    permission — warning if the script doesn't document this in a header
    comment. Otherwise the user runs it once, sees a permission prompt
    they don't understand, and the script appears broken.
  * Scripts reading the filesystem outside the user's own folder need
    Full Disk Access — warning if undocumented.
  * Use of `do shell script` with elevated privileges (`with administrator
    privileges`) prompting for the user's password — warning. Document
    why elevation is needed; users should not be asked to type their
    password into a script they don't understand.

  Robustness
  ----------
  * UI scripting that hard-codes element indices (`click button 1 of
    window 1`) — warning. Indices change between app versions; prefer
    `button "OK" of window 1` or named UI elements.
  * Long `delay` calls (`delay 5`) as synchronization with UI
    operations — warning. Brittle; prefer `repeat until exists ...`.
  * Hard-coded file paths using POSIX format mixed with HFS+ format
    — warning. Pick one and stick to it.
"""


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
