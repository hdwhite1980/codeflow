"""
test_guardian.py
================

Tests for the Code Guardian. Uses a fake Ollama client that returns
pre-canned JSON responses — proves the guardian logic, prompt construction,
response parsing, and ledger writes all work without standing up a real
local model.

What the fake covers
--------------------
- Well-formed JSON responses (the happy path).
- Responses wrapped in ```json fences (a real failure mode of small models).
- Responses with surrounding prose (also real).
- Malformed responses (the guardian should not crash, just return None or
  an empty result).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

from guardian import (
    Anomaly, CodeGuardian, RiskAssessment, SymbolSummary,
    _safe_parse_json, _strip_code_fences,
)
from ledger import ArtifactKind, EdgeKind, Tier
from ledger_memory import InMemoryLedgerStore
from runtime_sync import DatabaseConnector, GitConnector
from test_runtime_sync import FakeSupabase, FakeGithub


# ---------------------------------------------------------------------------
# Fake Ollama client. Stores a queue of canned responses; each .generate()
# call pops the next one. This lets us simulate the order of calls a complex
# operation makes and exercise both happy and failure paths.
# ---------------------------------------------------------------------------

@dataclass
class FakeOllamaClient:
    base_url: str = "fake://localhost"
    auth_token: str = "fake"
    model: str = "fake-model"
    timeout_seconds: float = 1.0

    def __post_init__(self) -> None:
        self.responses: list[str] = []
        self.calls: list[dict] = []

    def queue(self, response: str) -> None:
        self.responses.append(response)

    async def generate(
        self, prompt: str, *, system=None, temperature=0.2, max_tokens=1024,
    ) -> str:
        self.calls.append({
            "prompt": prompt, "system": system,
            "temperature": temperature, "max_tokens": max_tokens,
        })
        if not self.responses:
            raise RuntimeError("FakeOllamaClient ran out of queued responses")
        return self.responses.pop(0)

    async def healthy(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# Unit tests for the JSON parsing helpers (the resilient bit)
# ---------------------------------------------------------------------------

def test_strip_code_fences_handles_three_forms():
    # Plain JSON, no fences.
    assert _strip_code_fences('{"x": 1}') == '{"x": 1}'
    # Fenced with language hint.
    assert _strip_code_fences('```json\n{"x": 1}\n```') == '{"x": 1}'
    # Fenced without language hint.
    assert _strip_code_fences('```\n{"x": 1}\n```') == '{"x": 1}'
    print("  ✓ strip_code_fences_handles_three_forms")


def test_safe_parse_json_extracts_from_prose():
    """Small models sometimes emit 'Here is the JSON: {...} let me know if...'.
    We should still recover the object."""
    raw = 'Sure! Here is the analysis:\n{"purpose": "test", "inputs": []}\nLet me know if you need more.'
    parsed = _safe_parse_json(raw)
    assert parsed is not None, "should extract JSON from surrounding prose"
    assert parsed["purpose"] == "test"
    print("  ✓ safe_parse_json_extracts_from_prose")


def test_safe_parse_json_returns_none_for_garbage():
    assert _safe_parse_json("not json at all") is None
    assert _safe_parse_json("") is None
    print("  ✓ safe_parse_json_returns_none_for_garbage")


# ---------------------------------------------------------------------------
# Helper: build a project state the guardian can operate on
# ---------------------------------------------------------------------------

def _setup_project_with_symbols() -> tuple[InMemoryLedgerStore, str, FakeOllamaClient]:
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test guardian")
    db = DatabaseConnector(store, pid, "db:supabase_main", FakeSupabase())
    db.reconcile()
    git = GitConnector(store, pid, "hugh/myapp", "main", FakeGithub(), db)
    git.ingest_commit(FakeGithub().commits[0])
    return store, pid, FakeOllamaClient()


# ---------------------------------------------------------------------------
# Responsibility 1: continuous understanding
# ---------------------------------------------------------------------------

def test_summarize_symbol_writes_durable_summary_to_ledger():
    store, pid, ollama = _setup_project_with_symbols()
    guardian = CodeGuardian(store=store, project_id=pid, ollama=ollama)

    ollama.queue(json.dumps({
        "purpose": "Fetches a user's profile by id, including their email and legacy username.",
        "inputs": ["id: string (UUID)"],
        "outputs": ["the first row from users matching id, or undefined"],
        "assumptions": ["the database client is initialized", "id is a valid UUID"],
        "failure_modes": ["throws if the db query rejects", "returns undefined for deleted users"],
        "patterns_followed": ["uses the .from().select() ORM pattern"],
    }))

    # getUserProfile is the symbol from the runtime sync test fixture.
    symbol_key = "func:src/api/users.ts:getUserProfile"
    summary = asyncio.run(guardian.summarize_symbol(symbol_key))

    assert summary is not None
    assert "fetches" in summary.purpose.lower()
    assert summary.assumptions, "should capture assumptions"
    assert summary.failure_modes, "should capture failure modes"

    # The summary should now be readable from the ledger as a permanent record.
    persisted = guardian.get_summary(symbol_key)
    assert persisted is not None
    assert persisted.purpose == summary.purpose
    print("  ✓ summarize_symbol_writes_durable_summary_to_ledger")


def test_summarize_symbol_survives_fenced_response():
    """Real failure mode: model wraps JSON in ```json``` despite the prompt."""
    store, pid, ollama = _setup_project_with_symbols()
    guardian = CodeGuardian(store=store, project_id=pid, ollama=ollama)

    raw_fenced = "```json\n" + json.dumps({
        "purpose": "Updates a user's email.",
        "inputs": ["id", "email"], "outputs": [],
        "assumptions": [], "failure_modes": [], "patterns_followed": [],
    }) + "\n```"
    ollama.queue(raw_fenced)

    summary = asyncio.run(guardian.summarize_symbol(
        "func:src/api/users.ts:updateUserEmail"
    ))
    assert summary is not None
    assert "email" in summary.purpose.lower()
    print("  ✓ summarize_symbol_survives_fenced_response")


def test_summarize_symbol_returns_none_for_garbage_response():
    """Resilience: a broken LLM response should not crash the pipeline."""
    store, pid, ollama = _setup_project_with_symbols()
    guardian = CodeGuardian(store=store, project_id=pid, ollama=ollama)
    ollama.queue("Sorry, I cannot help with that request.")
    summary = asyncio.run(guardian.summarize_symbol(
        "func:src/api/users.ts:getUserProfile"
    ))
    assert summary is None, "garbage should return None, not raise"
    print("  ✓ summarize_symbol_returns_none_for_garbage_response")


# ---------------------------------------------------------------------------
# Responsibility 2: change risk analysis (the headline operation)
# ---------------------------------------------------------------------------

def test_assess_change_risk_combines_structural_and_semantic():
    """The headline scenario: user proposes dropping legacy_username; the
    guardian produces a risk report richer than the pure structural impact."""
    store, pid, ollama = _setup_project_with_symbols()
    guardian = CodeGuardian(store=store, project_id=pid, ollama=ollama)

    # Pre-populate summaries for the affected symbols. In production these
    # would have been written when the symbols first landed.
    ollama.queue(json.dumps({
        "purpose": "Fetches user profile including legacy_username.",
        "inputs": ["id"], "outputs": ["user row"],
        "assumptions": ["legacy_username column exists"],
        "failure_modes": ["throws if column missing"],
        "patterns_followed": [],
    }))
    asyncio.run(guardian.summarize_symbol(
        "func:src/api/users.ts:getUserProfile"
    ))

    ollama.queue(json.dumps({
        "purpose": "Looks up a user by their legacy username field via raw SQL.",
        "inputs": ["username: string"], "outputs": ["user row"],
        "assumptions": ["legacy_username column exists in users"],
        "failure_modes": ["sql throws if column dropped"],
        "patterns_followed": ["uses raw SQL instead of the ORM — outlier in this file"],
    }))
    asyncio.run(guardian.summarize_symbol(
        "func:src/api/users.ts:UserService.findByLegacy"
    ))

    # Now the risk assessment itself.
    ollama.queue(json.dumps({
        "overall_risk": "high",
        "specific_concerns": [
            {
                "symbol": "func:src/api/users.ts:UserService.findByLegacy",
                "concern": "Uses raw SQL referencing legacy_username; will throw immediately on column drop.",
                "severity": "high",
            },
            {
                "symbol": "func:src/api/users.ts:getUserProfile",
                "concern": "Selects legacy_username explicitly in the projection; will return undefined for the field but not crash.",
                "severity": "medium",
            },
        ],
        "migration_steps": [
            "Remove legacy_username from getUserProfile's select projection.",
            "Replace findByLegacy with an email-based lookup or remove if unused.",
            "Deploy the code changes.",
            "Only then drop the column.",
        ],
        "things_to_test_first": [
            "Login flow still works without legacy_username in the profile response.",
            "No remaining code paths call findByLegacy.",
        ],
    }))

    risk = asyncio.run(guardian.assess_change_risk(
        target_artifact_key="column:db:supabase_main:users.legacy_username",
        description="Drop legacy_username column from users table",
    ))

    assert risk.overall_risk == "high"
    assert len(risk.specific_concerns) == 2
    assert risk.migration_steps, "should produce migration steps"
    assert any("findByLegacy" in c.get("symbol", "") for c in risk.specific_concerns), \
        "should call out the raw-SQL function as the high-severity concern"
    # The structural report should still be included for the deterministic answer.
    assert risk.structural_report.severity == "medium", \
        "structural severity is medium (no deploy yet); guardian elevates it to high"
    print("  ✓ assess_change_risk_combines_structural_and_semantic")


def test_assess_change_risk_persists_to_ledger_for_audit_trail():
    """The risk assessment must be durable — when a customer audits why the
    team approved a breaking change, the guardian's verdict at that moment
    has to be in the ledger."""
    store, pid, ollama = _setup_project_with_symbols()
    guardian = CodeGuardian(store=store, project_id=pid, ollama=ollama)

    ollama.queue(json.dumps({
        "overall_risk": "low",
        "specific_concerns": [],
        "migration_steps": ["Just apply the migration."],
        "things_to_test_first": [],
    }))

    asyncio.run(guardian.assess_change_risk(
        target_artifact_key="column:db:supabase_main:users.email",
        description="Add a default value to email",
    ))

    # Find audit verdict entries authored by the guardian.
    audits = [
        e for e in store.all_current(pid, ArtifactKind.AUDIT_VERDICT)
        if e.author.startswith("guardian:")
    ]
    assert len(audits) == 1
    assert "risk:" in audits[0].artifact_key
    print("  ✓ assess_change_risk_persists_to_ledger_for_audit_trail")


# ---------------------------------------------------------------------------
# Responsibility 3: ambient sanity checks
# ---------------------------------------------------------------------------

def test_check_for_anomalies_flags_pattern_deviations():
    store, pid, ollama = _setup_project_with_symbols()
    guardian = CodeGuardian(store=store, project_id=pid, ollama=ollama)

    # Simulate a new file landing that doesn't follow the established pattern.
    new_file_body = """
import { db } from './db';

export async function getAllOrders() {
  // Anomaly: no tenant filter — every other query in this project filters by tenant.
  return db.from('orders').select('*');
}
"""
    store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE,
        "file:src/api/orders.ts", new_file_body,
        rationale="new orders endpoint", author="claude",
    )

    ollama.queue(json.dumps({
        "anomalies": [
            {
                "line_hint": "getAllOrders function",
                "description": "Query against orders table has no tenant filter.",
                "established_pattern": "Every other query on a tenant-scoped table in this project includes a .eq('tenant_id', currentTenant) call.",
                "severity": "error",
            }
        ]
    }))

    anomalies = asyncio.run(guardian.check_for_anomalies(
        "file:src/api/orders.ts"
    ))
    assert len(anomalies) == 1
    assert anomalies[0].severity == "error"
    assert "tenant" in anomalies[0].description.lower()
    print("  ✓ check_for_anomalies_flags_pattern_deviations")


def test_check_for_anomalies_records_empty_scan_for_audit_trail():
    """Even when nothing is wrong, the scan should be recorded — proves to
    customers that the guardian actually looked at every commit, not just
    the ones it flagged."""
    store, pid, ollama = _setup_project_with_symbols()
    guardian = CodeGuardian(store=store, project_id=pid, ollama=ollama)

    ollama.queue(json.dumps({"anomalies": []}))

    anomalies = asyncio.run(guardian.check_for_anomalies(
        "file:src/api/users.ts"
    ))
    assert anomalies == []

    # The empty scan should still be persisted.
    scan_entry = store.current_entry(
        pid, "anomalies:file:src/api/users.ts"
    )
    assert scan_entry is not None
    assert scan_entry.author.startswith("guardian:")
    print("  ✓ check_for_anomalies_records_empty_scan_for_audit_trail")


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

def run_all():
    tests = [
        test_strip_code_fences_handles_three_forms,
        test_safe_parse_json_extracts_from_prose,
        test_safe_parse_json_returns_none_for_garbage,
        test_summarize_symbol_writes_durable_summary_to_ledger,
        test_summarize_symbol_survives_fenced_response,
        test_summarize_symbol_returns_none_for_garbage_response,
        test_assess_change_risk_combines_structural_and_semantic,
        test_assess_change_risk_persists_to_ledger_for_audit_trail,
        test_check_for_anomalies_flags_pattern_deviations,
        test_check_for_anomalies_records_empty_scan_for_audit_trail,
    ]
    print(f"Running {len(tests)} guardian tests...")
    for t in tests:
        t()
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    run_all()
