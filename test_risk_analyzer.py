"""Tests for guardian_pipeline.analyze_change_risk (Turn D).

We test:
  - Risk prompt construction (right context shape)
  - JSON parsing tolerance (model returns malformed output)
  - Concern/severity coercion (model lies about types)
  - Confidence clamping
  - Ledger round-trip: persist + read back
  - Sequencing of risk query numbers
  - Empty impact set still produces a usable assessment
  - Pluggability: analyzer works against any client with .complete()
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from guardian_pipeline import (
    FileSummary,
    RiskAssessment,
    RiskConcern,
    analyze_change_risk,
    list_risk_assessments,
    next_risk_seq,
    write_file_summary,
    write_risk_assessment,
    _build_risk_prompt,
)
from ledger_memory import InMemoryLedgerStore


def _run(coro):
    return asyncio.run(coro)


@dataclass
class _FakeResult:
    text: str
    model: str = "qwen2.5-coder:7b"
    stop_reason: str = "end_turn"
    input_tokens: int = 200
    output_tokens: int = 400


class _FakeClient:
    """LLM client double. Returns a controlled response text. Records
    the last prompt for assertion."""
    def __init__(self, response_text: str, model: str = "qwen2.5-coder:7b") -> None:
        self.response_text = response_text
        self.model = model
        self.last_prompt = ""
        self.last_system = ""

    async def complete(self, prompt: str, *, system=None,
                       max_tokens=1024, temperature=0.2, **kw):
        self.last_prompt = prompt
        self.last_system = system or ""
        return _FakeResult(text=self.response_text, model=self.model)


def _seed_summaries(store, pid, files):
    for path, plain, purpose, risk in files:
        write_file_summary(store, pid, FileSummary(
            file_path=path, plain_english=plain, technical="t",
            purpose=purpose, touches=["state"], assumes=["env set"],
            failure_modes=["bad input"], risk_notes=[risk] if risk else [],
            indexed_at=0.0, indexer_model="qwen2.5-coder:7b",
            input_tokens=10, output_tokens=20,
        ))


# ---------------------------------------------------------------------------
# Prompt construction.
# ---------------------------------------------------------------------------

class TestRiskPromptConstruction(unittest.TestCase):
    def test_includes_target_and_change_description(self):
        prompt = _build_risk_prompt(
            target="app/auth.py",
            change_description="rename verify_password to check_password",
            impact_set=[], summaries={},
        )
        self.assertIn("app/auth.py", prompt)
        self.assertIn("rename verify_password", prompt)

    def test_includes_impact_set(self):
        prompt = _build_risk_prompt(
            target="app/db.py",
            change_description="drop tenant_id column",
            impact_set=["app/main.py", "app/auth.py", "app/routers/x.py"],
            summaries={},
        )
        for p in ("app/main.py", "app/auth.py", "app/routers/x.py"):
            self.assertIn(p, prompt)

    def test_empty_impact_set_notes_absence(self):
        prompt = _build_risk_prompt(
            target="app/x.py", change_description="x",
            impact_set=[], summaries={},
        )
        self.assertIn("none", prompt.lower())

    def test_truncates_large_impact_set(self):
        big_set = [f"file_{i}.py" for i in range(80)]
        prompt = _build_risk_prompt(
            target="x", change_description="x",
            impact_set=big_set, summaries={},
        )
        self.assertIn("truncated", prompt)
        # 50 listed, 30 omitted.
        self.assertIn("30 more", prompt)

    def test_summaries_render_with_purpose_and_failure_modes(self):
        prompt = _build_risk_prompt(
            target="app/auth.py", change_description="change",
            impact_set=["app/main.py"],
            summaries={
                "app/main.py": {
                    "purpose": "HTTP entry point",
                    "touches": ["routing", "middleware"],
                    "assumes": ["PORT env"],
                    "failure_modes": ["DB unreachable at boot"],
                    "risk_notes": ["no /healthz"],
                }
            }
        )
        self.assertIn("HTTP entry point", prompt)
        self.assertIn("DB unreachable", prompt)
        self.assertIn("routing", prompt)

    def test_no_summaries_lowers_confidence_hint(self):
        prompt = _build_risk_prompt(
            target="x", change_description="x",
            impact_set=["a.py", "b.py"],
            summaries={},
        )
        self.assertIn("no semantic summaries", prompt.lower())


# ---------------------------------------------------------------------------
# analyze_change_risk — end-to-end with fake client.
# ---------------------------------------------------------------------------

class TestAnalyzeChangeRisk(unittest.TestCase):
    def test_happy_path(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _seed_summaries(store, pid, [
            ("app/auth.py", "Auth handler", "Verify passwords", "rate limit missing"),
        ])

        response = (
            '{"severity":"high",'
            '"plain_narrative":"Renaming will break login for some users.",'
            '"technical_narrative":"The verify_password function in app/auth.py is called by the login route handler. Renaming requires updating both call sites and any tests.",'
            '"affected_paths":["app/auth.py"],'
            '"concerns":[{"path":"app/auth.py","reason":"All callers must be updated simultaneously.","severity":"high"}],'
            '"suggested_sequencing":["Update tests first","Then rename in auth.py","Then update callers"],'
            '"confidence":0.85}'
        )
        client = _FakeClient(response)

        assessment = _run(analyze_change_risk(
            store=store, project_id=pid,
            target="app/auth.py",
            change_description="rename verify_password to check_password",
            client=client,
        ))
        self.assertEqual(assessment.severity, "high")
        self.assertIn("login", assessment.plain_narrative.lower())
        self.assertEqual(len(assessment.concerns), 1)
        self.assertEqual(assessment.concerns[0].severity, "high")
        self.assertEqual(len(assessment.suggested_sequencing), 3)
        self.assertAlmostEqual(assessment.confidence, 0.85)
        self.assertEqual(assessment.indexed_summary_count, 1)

    def test_accepts_bare_path_target(self):
        """Customer enters 'app/main.py'; we should auto-prefix to
        'file:<pid>:app/main.py' before walking the graph."""
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        client = _FakeClient(
            '{"severity":"low","plain_narrative":"x","technical_narrative":"y",'
            '"affected_paths":[],"concerns":[],"suggested_sequencing":[],'
            '"confidence":0.5}'
        )
        assessment = _run(analyze_change_risk(
            store=store, project_id=pid,
            target="app/main.py",
            change_description="add /metrics endpoint",
            client=client,
        ))
        # The target was normalized to the bare path for display.
        self.assertEqual(assessment.target, "app/main.py")

    def test_accepts_fully_qualified_artifact_key(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        client = _FakeClient(
            '{"severity":"low","plain_narrative":"","technical_narrative":"",'
            '"affected_paths":[],"concerns":[],"suggested_sequencing":[],'
            '"confidence":0.5}'
        )
        target_key = f"file:{pid}:app/main.py"
        assessment = _run(analyze_change_risk(
            store=store, project_id=pid,
            target=target_key,
            change_description="change",
            client=client,
        ))
        # Whether the user passed bare path or fully-qualified, the
        # response shows the readable form.
        self.assertEqual(assessment.target, "app/main.py")

    def test_malformed_response_does_not_crash(self):
        """If the model returns garbage JSON, we still produce an
        assessment with defaults rather than raising. The narrative
        will reflect the parse failure so users know."""
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        client = _FakeClient("definitely not JSON")
        assessment = _run(analyze_change_risk(
            store=store, project_id=pid,
            target="x.py",
            change_description="change",
            client=client,
        ))
        # Severity defaults to "low" on parse failure.
        self.assertEqual(assessment.severity, "low")

    def test_invalid_severity_coerces_to_low(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        # Model says severity="catastrophic" which isn't in our ladder.
        client = _FakeClient(
            '{"severity":"catastrophic","plain_narrative":"","technical_narrative":"",'
            '"affected_paths":[],"concerns":[],"suggested_sequencing":[],'
            '"confidence":0.5}'
        )
        assessment = _run(analyze_change_risk(
            store=store, project_id=pid, target="x",
            change_description="x", client=client,
        ))
        self.assertEqual(assessment.severity, "low")

    def test_concern_severity_coerced(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        client = _FakeClient(
            '{"severity":"high","plain_narrative":"","technical_narrative":"",'
            '"affected_paths":["a.py"],'
            '"concerns":['
            '{"path":"a.py","reason":"r","severity":"NUCLEAR"},'
            '{"path":"b.py","reason":"r2","severity":"medium"}'
            '],"suggested_sequencing":[],"confidence":0.7}'
        )
        assessment = _run(analyze_change_risk(
            store=store, project_id=pid, target="x",
            change_description="x", client=client,
        ))
        self.assertEqual(assessment.concerns[0].severity, "medium")  # default
        self.assertEqual(assessment.concerns[1].severity, "medium")

    def test_confidence_clamped(self):
        """Models can return confidence outside [0,1]. Clamp it."""
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        client = _FakeClient(
            '{"severity":"low","plain_narrative":"","technical_narrative":"",'
            '"affected_paths":[],"concerns":[],"suggested_sequencing":[],'
            '"confidence":2.5}'
        )
        assessment = _run(analyze_change_risk(
            store=store, project_id=pid, target="x",
            change_description="x", client=client,
        ))
        self.assertLessEqual(assessment.confidence, 1.0)
        self.assertGreaterEqual(assessment.confidence, 0.0)

    def test_confidence_non_numeric_defaults(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        client = _FakeClient(
            '{"severity":"low","plain_narrative":"","technical_narrative":"",'
            '"affected_paths":[],"concerns":[],"suggested_sequencing":[],'
            '"confidence":"high"}'
        )
        assessment = _run(analyze_change_risk(
            store=store, project_id=pid, target="x",
            change_description="x", client=client,
        ))
        self.assertEqual(assessment.confidence, 0.5)  # safe default

    def test_concerns_capped_at_10(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        many_concerns = ",".join(
            f'{{"path":"f{i}.py","reason":"r{i}","severity":"low"}}'
            for i in range(20)
        )
        client = _FakeClient(
            '{"severity":"low","plain_narrative":"","technical_narrative":"",'
            f'"affected_paths":[],"concerns":[{many_concerns}],'
            '"suggested_sequencing":[],"confidence":0.5}'
        )
        assessment = _run(analyze_change_risk(
            store=store, project_id=pid, target="x",
            change_description="x", client=client,
        ))
        self.assertLessEqual(len(assessment.concerns), 10)

    def test_indexed_summary_count_reflects_real_data(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _seed_summaries(store, pid, [
            ("a.py", "A", "purpose A", "risk A"),
            ("b.py", "B", "purpose B", "risk B"),
            ("c.py", "C", "purpose C", "risk C"),
        ])
        client = _FakeClient(
            '{"severity":"low","plain_narrative":"","technical_narrative":"",'
            '"affected_paths":[],"concerns":[],"suggested_sequencing":[],'
            '"confidence":0.5}'
        )
        # Asking about a.py. Impact set is empty in InMemoryLedgerStore
        # (no graph edges seeded), so only a.py itself is summarized.
        assessment = _run(analyze_change_risk(
            store=store, project_id=pid, target="a.py",
            change_description="change", client=client,
        ))
        # Just the target's own summary.
        self.assertEqual(assessment.indexed_summary_count, 1)


# ---------------------------------------------------------------------------
# Persistence: write + read back risk assessments.
# ---------------------------------------------------------------------------

class TestRiskPersistence(unittest.TestCase):
    def test_write_and_read_back(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        assessment = RiskAssessment(
            target="app/auth.py",
            change_description="rename verify_password",
            severity="high",
            plain_narrative="May break logins",
            technical_narrative="verify_password is widely called",
            affected_paths=["app/auth.py", "app/main.py"],
            concerns=[
                RiskConcern(path="app/auth.py",
                           reason="callers must update too", severity="high"),
            ],
            suggested_sequencing=["Update tests", "Rename function"],
            confidence=0.85,
            analyzer_model="qwen2.5-coder:7b",
            input_tokens=200, output_tokens=400,
            indexed_summary_count=2,
        )
        write_risk_assessment(store, pid, assessment, seq=1)

        risks = list_risk_assessments(store, pid)
        self.assertEqual(len(risks), 1)
        r = risks[0]
        self.assertEqual(r["seq"], 1)
        self.assertEqual(r["target"], "app/auth.py")
        self.assertEqual(r["severity"], "high")
        self.assertEqual(len(r["concerns"]), 1)
        self.assertEqual(r["concerns"][0]["path"], "app/auth.py")

    def test_history_newest_first(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        import time as time_mod
        for i in range(1, 4):
            a = RiskAssessment(
                target=f"file_{i}.py",
                change_description=f"change {i}",
                severity="low", plain_narrative="x", technical_narrative="y",
                affected_paths=[], concerns=[], suggested_sequencing=[],
                confidence=0.5, analyzer_model="m",
                input_tokens=0, output_tokens=0, indexed_summary_count=0,
            )
            write_risk_assessment(store, pid, a, seq=i)
            time_mod.sleep(0.01)  # ensure asked_at differs

        risks = list_risk_assessments(store, pid)
        self.assertEqual(len(risks), 3)
        # Newest first by seq number.
        self.assertEqual(risks[0]["seq"], 3)
        self.assertEqual(risks[2]["seq"], 1)


class TestRiskSeqAllocation(unittest.TestCase):
    def test_first_seq_is_one(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        self.assertEqual(next_risk_seq(store, pid), 1)

    def test_increments_on_each_write(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        a = RiskAssessment(
            target="x", change_description="x",
            severity="low", plain_narrative="", technical_narrative="",
            affected_paths=[], concerns=[], suggested_sequencing=[],
            confidence=0.5, analyzer_model="m",
            input_tokens=0, output_tokens=0, indexed_summary_count=0,
        )
        self.assertEqual(next_risk_seq(store, pid), 1)
        write_risk_assessment(store, pid, a, seq=1)
        self.assertEqual(next_risk_seq(store, pid), 2)
        write_risk_assessment(store, pid, a, seq=2)
        self.assertEqual(next_risk_seq(store, pid), 3)

    def test_unrelated_decision_records_dont_inflate_seq(self):
        """`guardian:risk:*` keys are counted; other decision records aren't."""
        from ledger import ArtifactKind, Tier
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        # Write a non-risk decision record.
        store.write_entry(
            project_id=pid, tier=Tier.SPEC,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key="iteration:1:plan",
            body={"x": "y"}, rationale="iteration plan",
            author="test",
        )
        self.assertEqual(next_risk_seq(store, pid), 1)


if __name__ == "__main__":
    unittest.main()
