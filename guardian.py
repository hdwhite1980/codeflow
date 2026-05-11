"""
codeflow.guardian
=================

The Code Guardian. A local-LLM-powered service that maintains semantic
understanding of a project alongside the structural ledger, and answers
change-risk questions that pure graph traversal can't.

What it is NOT
--------------
Not a generator. Not part of the build pipeline. Not in the multi-AI vote
critical path. The guardian is a continuously-running companion that
reads, understands, and reasons — it never writes app code.

Three responsibilities
----------------------
1. Continuous understanding: as files land in the ledger, the guardian
   produces semantic summaries — what each symbol does, what assumptions
   it makes, what failure modes it has — and writes them back as
   SEMANTIC_SUMMARY artifacts. Durable knowledge.
2. Change risk analysis: given a proposed change, the guardian combines
   the structural impact_set (from the graph) with semantic reasoning
   over the affected symbols to produce a richer risk report.
3. Ambient sanity checks: as new commits arrive, the guardian compares
   them against the established patterns of the codebase and flags
   semantic anomalies that aren't structural violations (a new query
   missing the tenant filter every other query has, a route that returns
   raw DB rows when every other route uses a DTO, etc.).

Why local
---------
Three reasons that all compound:
- Privacy: MSP and regulated customers don't want every commit sent to
  a frontier API for review.
- Latency: short Q&A round trips are faster locally than over the
  internet to a frontier provider.
- Cost: this work is constant and small; running it locally is cheaper
  than paying per-token forever.

Model choice
------------
The default model is qwen2.5-coder:7b — small enough to run on a CPU
Hetzner box, smart enough to summarize and reason about short code
contexts. Swap to :32b on a GPU box for stronger analysis. The Ollama
client abstracts this; the guardian doesn't care which model is behind it.

Prompt shape
------------
All prompts are JSON-structured with a clear schema for the response.
We do NOT free-form chat with the model. Three reasons: 1) the responses
need to be machine-readable so we can write them to the ledger,
2) structured responses are dramatically more reliable from small models,
3) we want the audit trail to show what was actually asked, not the model's
narrative of what it thought it was doing.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from hetzner_client import OllamaClient
from ledger import ArtifactKind, EdgeKind, Tier
from runtime_sync import ChangeImpactAnalyzer, ImpactReport


# ---------------------------------------------------------------------------
# Response schemas — what the LLM must return for each task type. Keeping
# these as constants documents the contract and lets us validate responses
# before writing them to the ledger.
# ---------------------------------------------------------------------------

SUMMARY_SCHEMA = """
{
  "purpose": "<one sentence describing what this symbol does>",
  "inputs": ["<expected inputs, one per item>"],
  "outputs": ["<what it returns or mutates, one per item>"],
  "assumptions": ["<implicit assumptions this code makes>"],
  "failure_modes": ["<ways this can fail at runtime>"],
  "patterns_followed": ["<conventions from the rest of the codebase>"]
}
"""

RISK_SCHEMA = """
{
  "overall_risk": "low" | "medium" | "high" | "critical",
  "specific_concerns": [
    {
      "symbol": "<artifact_key of the symbol>",
      "concern": "<plain-English description>",
      "severity": "low" | "medium" | "high"
    }
  ],
  "migration_steps": ["<ordered steps to execute the change safely>"],
  "things_to_test_first": ["<test cases that should pass before shipping>"]
}
"""

ANOMALY_SCHEMA = """
{
  "anomalies": [
    {
      "line_hint": "<line range or symbol name>",
      "description": "<what is unusual>",
      "established_pattern": "<the convention this deviates from>",
      "severity": "info" | "warning" | "error"
    }
  ]
}
"""


# ---------------------------------------------------------------------------
# Output dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SymbolSummary:
    symbol_key: str
    purpose: str
    inputs: list[str]
    outputs: list[str]
    assumptions: list[str]
    failure_modes: list[str]
    patterns_followed: list[str]

    def to_dict(self) -> dict:
        return {
            "symbol_key": self.symbol_key,
            "purpose": self.purpose,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "assumptions": self.assumptions,
            "failure_modes": self.failure_modes,
            "patterns_followed": self.patterns_followed,
        }


@dataclass
class RiskAssessment:
    """The guardian's enriched answer to 'if this changes, what breaks'.
    This pairs with the structural ImpactReport — the graph tells you what's
    affected, the guardian tells you what to actually worry about."""
    target_key: str
    description: str
    overall_risk: str
    specific_concerns: list[dict]
    migration_steps: list[str]
    things_to_test_first: list[str]
    structural_report: ImpactReport      # the ChangeImpactAnalyzer output

    def to_dict(self) -> dict:
        return {
            "target": self.target_key,
            "description": self.description,
            "overall_risk": self.overall_risk,
            "specific_concerns": self.specific_concerns,
            "migration_steps": self.migration_steps,
            "things_to_test_first": self.things_to_test_first,
            "structural_impact": self.structural_report.to_dict(),
        }


@dataclass
class Anomaly:
    file_key: str
    line_hint: str
    description: str
    established_pattern: str
    severity: str

    def to_dict(self) -> dict:
        return {
            "file_key": self.file_key,
            "line_hint": self.line_hint,
            "description": self.description,
            "established_pattern": self.established_pattern,
            "severity": self.severity,
        }


# ---------------------------------------------------------------------------
# Prompt templates. These are deliberately verbose — small models do much
# better with explicit instructions and concrete examples than with terse
# prompts.
# ---------------------------------------------------------------------------

SUMMARIZE_PROMPT_SYSTEM = """\
You are the Code Guardian for a software project. Your job is to read code \
and produce structured semantic summaries that will be stored in a project \
ledger as durable knowledge.

You ALWAYS respond with valid JSON matching the requested schema. Never \
include explanation, markdown formatting, or text outside the JSON. If you \
cannot produce a useful answer, return a JSON object with empty arrays \
rather than refusing.

Your summaries are read later by other AIs and by human reviewers. Write \
clearly and concretely. Avoid generic statements like "this function does \
something with users" — say what it actually does.
"""


def build_summary_prompt(
    symbol_key: str,
    symbol_kind: str,
    symbol_body: str,
    surrounding_context: str,
    related_ledger_entries: list[dict],
) -> str:
    """Construct the user-side prompt for a symbol summary."""
    ledger_block = ""
    if related_ledger_entries:
        ledger_block = (
            "\nFor context, here is what the project ledger says about "
            "related artifacts:\n"
            + "\n".join(
                f"- {e['artifact_key']}: {e['rationale']}"
                for e in related_ledger_entries[:5]   # cap to keep prompt small
            )
        )

    return f"""\
Analyze the following {symbol_kind} from the project codebase and produce \
a structured summary.

Symbol identity: {symbol_key}

Symbol body:
```
{symbol_body}
```

Surrounding file context (for reference only — summarize the symbol, not \
the whole file):
```
{surrounding_context[:2000]}
```
{ledger_block}

Respond with a single JSON object matching this schema exactly:
{SUMMARY_SCHEMA}

Important guidance:
- "assumptions" should capture implicit requirements (e.g. "expects \
input id to be a valid UUID", "expects database connection to be open").
- "failure_modes" should be concrete (e.g. "throws if email is malformed", \
"returns null for deleted users", NOT "could fail").
- "patterns_followed" should reference how this fits the rest of the \
codebase if you can tell from the context.
"""


SUMMARIZE_PROMPT_TEMPLATE_NOTE = """\
Keep total prompt length under ~6000 chars; small models lose focus with \
walls of context. We trim aggressively in the caller.
"""


RISK_PROMPT_SYSTEM = """\
You are the Code Guardian for a software project. A user is proposing to \
change part of the system. You have access to the structural impact set \
(everything that depends on the change target) and to semantic summaries \
of the affected code.

Your job is to produce a risk assessment that goes BEYOND the structural \
list — explain what could actually go wrong, in what order to make the \
change safely, and what to test first.

You ALWAYS respond with valid JSON matching the requested schema. Never \
include explanation outside the JSON.

Be specific. "There is risk" is useless. "The session middleware in \
auth.ts reads legacy_username during token refresh; if that column is \
gone before old tokens expire, all sessions break on next refresh" is \
useful.
"""


def build_risk_prompt(
    target_description: str,
    structural_impact: dict,
    affected_summaries: list[SymbolSummary],
) -> str:
    summary_block = ""
    if affected_summaries:
        summary_block = "\nSemantic summaries of affected symbols:\n" + "\n".join(
            f"- {s.symbol_key}\n"
            f"    purpose: {s.purpose}\n"
            f"    assumptions: {'; '.join(s.assumptions) or 'none recorded'}\n"
            f"    failure modes: {'; '.join(s.failure_modes) or 'none recorded'}"
            for s in affected_summaries[:8]   # cap to keep prompt manageable
        )

    return f"""\
Proposed change: {target_description}

Structural impact set (from the dependency graph):
{json.dumps(structural_impact, indent=2)[:2500]}
{summary_block}

Produce a risk assessment as a JSON object matching this schema exactly:
{RISK_SCHEMA}

Guidance:
- "overall_risk" is "critical" only when there's no safe rollback or when \
data loss is possible. "high" means the change can break running services. \
"medium" is normal app-breakage risk. "low" is forward-compatible.
- "specific_concerns" should reference the actual symbol_keys from the \
impact set. Do not invent symbols.
- "migration_steps" should be an ordered list. If the change is forward-\
compatible (e.g. adding a column), say so and leave steps minimal.
- "things_to_test_first" should be concrete test scenarios that would \
catch the most likely failures.
"""


ANOMALY_PROMPT_SYSTEM = """\
You are the Code Guardian. New code has just been committed to the project. \
Your job is to compare it against the patterns of the existing codebase and \
flag anything that deviates in suspicious ways.

You ALWAYS respond with valid JSON matching the requested schema.

A "suspicious deviation" is something a senior engineer would flag in code \
review but a linter wouldn't catch. Examples:
- A new database query that doesn't include the tenant filter all other \
queries on this table use.
- A new route that returns raw database rows when every other route uses \
a DTO/serializer.
- A new function that swallows exceptions when the project's convention \
is to bubble them up to a central handler.
- A new use of an env var that isn't declared in the project's config.

If you see no anomalies, return an empty list. Do not invent anomalies to \
justify your existence.
"""


def build_anomaly_prompt(
    new_file_path: str,
    new_file_body: str,
    similar_files: list[dict],
) -> str:
    similar_block = ""
    if similar_files:
        similar_block = (
            "\nExamples of similar files already in the project (for "
            "pattern comparison):\n"
            + "\n\n".join(
                f"--- {f['path']} ---\n{f['body'][:1500]}"
                for f in similar_files[:3]
            )
        )

    return f"""\
A new file has been committed: {new_file_path}

File body:
```
{new_file_body[:4000]}
```
{similar_block}

Identify any places where the new file deviates from established patterns \
in the project in ways that warrant review. Return a JSON object matching \
this schema exactly:
{ANOMALY_SCHEMA}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_code_fences(s: str) -> str:
    """Models sometimes wrap JSON in ```json fences despite instructions.
    Strip them defensively so we don't lose responses to that nit."""
    s = s.strip()
    # Match opening fence (```json or ```) and capture content up to closing fence
    m = re.match(r"^```(?:json)?\s*\n?(.*?)\n?```\s*$", s, re.DOTALL)
    if m:
        return m.group(1).strip()
    return s


def _safe_parse_json(raw: str) -> Optional[dict]:
    """Parse the LLM response defensively. Returns None on failure rather
    than raising — the caller decides what to do (skip, retry, log).

    Small models occasionally emit JSON with trailing commas, single quotes,
    or surrounding prose. We try a strict parse first; if that fails we make
    one or two cleanup attempts before giving up."""
    stripped = _strip_code_fences(raw)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        pass

    # Try extracting the first balanced JSON object from the response.
    depth = 0
    start = None
    for i, ch in enumerate(stripped):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                candidate = stripped[start : i + 1]
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    pass
    return None


# ---------------------------------------------------------------------------
# The guardian
# ---------------------------------------------------------------------------

@dataclass
class CodeGuardian:
    """
    The orchestration class. Takes the ledger store, the project id, and
    an Ollama client. Exposes three high-level operations matching the
    three responsibilities.
    """
    store: Any                       # LedgerStore or InMemoryLedgerStore
    project_id: str
    ollama: OllamaClient

    # ---- Responsibility 1: continuous understanding ----------------------

    async def summarize_symbol(
        self, symbol_key: str,
    ) -> Optional[SymbolSummary]:
        """
        Read a symbol from the ledger, produce a semantic summary, write
        the summary back to the ledger. Idempotent: re-running on the same
        symbol with the same code body produces a no-op (content-addressable
        storage dedupes the blob).
        """
        # Pull the symbol entry, its file, and a few related ledger entries
        # for context.
        symbol_entry = self.store.current_entry(self.project_id, symbol_key)
        if not symbol_entry:
            return None

        # Get the file the symbol lives in by walking the contains_symbol edge.
        file_edges = self.store.neighbors(
            self.project_id, symbol_key, direction="out",
            edge_kinds=[EdgeKind.CONTAINS_SYMBOL],
        )
        if not file_edges:
            return None
        file_key = file_edges[0].to_artifact_key
        file_entry = self.store.current_entry(self.project_id, file_key)
        if not file_entry:
            return None

        # Read the file body and slice out the symbol's body using line range.
        file_body_bytes, _ = self.store.get_blob(file_entry.blob_sha256)
        file_body = file_body_bytes.decode("utf-8", errors="replace")
        symbol_meta_bytes, _ = self.store.get_blob(symbol_entry.blob_sha256)
        symbol_meta = json.loads(symbol_meta_bytes)
        lines = file_body.split("\n")
        # line_start/end are 1-indexed and inclusive
        start = max(0, symbol_meta["line_start"] - 1)
        end = min(len(lines), symbol_meta["line_end"])
        symbol_body = "\n".join(lines[start:end])

        # Pull a small amount of contextual ledger info — the file's
        # rationale and a handful of related symbol summaries if they exist.
        related: list[dict] = [{
            "artifact_key": file_entry.artifact_key,
            "rationale": file_entry.rationale,
        }]

        prompt = build_summary_prompt(
            symbol_key=symbol_key,
            symbol_kind=symbol_meta.get("kind", "function"),
            symbol_body=symbol_body,
            surrounding_context=file_body,
            related_ledger_entries=related,
        )

        raw = await self.ollama.generate(
            prompt=prompt,
            system=SUMMARIZE_PROMPT_SYSTEM,
            temperature=0.1,         # low: we want consistent structured output
            max_tokens=800,
        )
        parsed = _safe_parse_json(raw)
        if not parsed:
            return None

        summary = SymbolSummary(
            symbol_key=symbol_key,
            purpose=str(parsed.get("purpose", ""))[:500],
            inputs=[str(x) for x in (parsed.get("inputs") or [])][:10],
            outputs=[str(x) for x in (parsed.get("outputs") or [])][:10],
            assumptions=[str(x) for x in (parsed.get("assumptions") or [])][:10],
            failure_modes=[str(x) for x in (parsed.get("failure_modes") or [])][:10],
            patterns_followed=[str(x) for x in (parsed.get("patterns_followed") or [])][:10],
        )

        # Persist as a DECISION_RECORD attached to the symbol. We use
        # decision_record (not a new kind) because it doesn't need its own
        # node — it's metadata about the symbol, and walking incoming edges
        # of the symbol shouldn't pick it up as a dependent.
        self.store.write_entry(
            project_id=self.project_id,
            tier=Tier.AUDIT,                  # guardian writes count as audit
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key=f"summary:{symbol_key}",
            body=summary.to_dict(),
            rationale=f"Guardian semantic summary of {symbol_key}",
            author="guardian:qwen-coder",
        )
        return summary

    def get_summary(self, symbol_key: str) -> Optional[SymbolSummary]:
        """Synchronous read of a previously-written summary."""
        entry = self.store.current_entry(
            self.project_id, f"summary:{symbol_key}",
        )
        if not entry:
            return None
        body_bytes, _ = self.store.get_blob(entry.blob_sha256)
        data = json.loads(body_bytes)
        return SymbolSummary(**data)

    # ---- Responsibility 2: change risk analysis --------------------------

    async def assess_change_risk(
        self, target_artifact_key: str, description: str,
    ) -> RiskAssessment:
        """
        The headline operation. Combines the structural impact set with
        semantic summaries of the affected symbols and asks the local LLM
        to produce a risk assessment.

        This is what `POST /api/projects/{id}/risk` should call. The pure
        structural ImpactReport is still useful (it's deterministic and
        fast), but this enriched version is what a developer actually
        wants to read before approving a change.
        """
        analyzer = ChangeImpactAnalyzer(self.store, self.project_id)
        structural = analyzer.analyze_change(target_artifact_key, description)

        # Pull summaries for affected symbols. If a summary doesn't exist
        # yet, we proceed without it — the guardian's first run on a new
        # project will be richer once summaries have populated.
        summaries: list[SymbolSummary] = []
        for sym_key in structural.affected_symbols[:8]:
            s = self.get_summary(sym_key)
            if s:
                summaries.append(s)

        prompt = build_risk_prompt(
            target_description=description,
            structural_impact=structural.to_dict(),
            affected_summaries=summaries,
        )
        raw = await self.ollama.generate(
            prompt=prompt,
            system=RISK_PROMPT_SYSTEM,
            temperature=0.2,
            max_tokens=1200,
        )
        parsed = _safe_parse_json(raw) or {}

        risk = RiskAssessment(
            target_key=target_artifact_key,
            description=description,
            overall_risk=parsed.get("overall_risk", structural.severity),
            specific_concerns=parsed.get("specific_concerns", []) or [],
            migration_steps=parsed.get("migration_steps", []) or [],
            things_to_test_first=parsed.get("things_to_test_first", []) or [],
            structural_report=structural,
        )

        # Record the assessment in the ledger so the audit trail captures
        # what the guardian said about this change at this point in time.
        self.store.write_entry(
            project_id=self.project_id,
            tier=Tier.AUDIT,
            artifact_kind=ArtifactKind.AUDIT_VERDICT,
            artifact_key=f"risk:{target_artifact_key}:{structural.severity}",
            body=risk.to_dict(),
            rationale=f"Guardian risk assessment for {description}",
            author="guardian:qwen-coder",
        )
        return risk

    # ---- Responsibility 3: ambient sanity checks -------------------------

    async def check_for_anomalies(
        self, new_file_key: str, max_similar_files: int = 3,
    ) -> list[Anomaly]:
        """
        Run when a new file lands. Pulls a handful of similar files from
        the project for pattern comparison and asks the LLM what looks off.

        "Similar" is currently defined by path prefix (files in the same
        directory). A future version can use embeddings for better
        similarity, but path prefix is a strong signal in well-organized
        projects and costs nothing.
        """
        new_entry = self.store.current_entry(self.project_id, new_file_key)
        if not new_entry:
            return []
        body_bytes, _ = self.store.get_blob(new_entry.blob_sha256)
        new_body = body_bytes.decode("utf-8", errors="replace")

        # Find similar files by path prefix.
        path = new_file_key.removeprefix("file:")
        if "/" in path:
            dir_prefix = "file:" + path.rsplit("/", 1)[0] + "/"
        else:
            dir_prefix = "file:"

        all_files = self.store.all_current(self.project_id, ArtifactKind.FILE)
        similar: list[dict] = []
        for f in all_files:
            if f.artifact_key == new_file_key:
                continue
            if not f.artifact_key.startswith(dir_prefix):
                continue
            body, _ = self.store.get_blob(f.blob_sha256)
            similar.append({
                "path": f.artifact_key.removeprefix("file:"),
                "body": body.decode("utf-8", errors="replace"),
            })
            if len(similar) >= max_similar_files:
                break

        prompt = build_anomaly_prompt(
            new_file_path=path,
            new_file_body=new_body,
            similar_files=similar,
        )
        raw = await self.ollama.generate(
            prompt=prompt,
            system=ANOMALY_PROMPT_SYSTEM,
            temperature=0.3,    # slightly higher; we want a bit of creativity
            max_tokens=800,
        )
        parsed = _safe_parse_json(raw) or {}

        anomalies = []
        for a in (parsed.get("anomalies") or [])[:10]:
            anomalies.append(Anomaly(
                file_key=new_file_key,
                line_hint=str(a.get("line_hint", ""))[:120],
                description=str(a.get("description", ""))[:500],
                established_pattern=str(a.get("established_pattern", ""))[:500],
                severity=str(a.get("severity", "info")),
            ))

        # Persist the anomaly report — even if empty, the audit trail
        # benefits from "we checked and saw nothing".
        self.store.write_entry(
            project_id=self.project_id,
            tier=Tier.AUDIT,
            artifact_kind=ArtifactKind.AUDIT_VERDICT,
            artifact_key=f"anomalies:{new_file_key}",
            body={"anomalies": [a.to_dict() for a in anomalies]},
            rationale=(
                f"Guardian anomaly scan for {new_file_key}: "
                f"{len(anomalies)} finding(s)"
            ),
            author="guardian:qwen-coder",
        )
        return anomalies
