"""Tests for the iteration/fix-all risk helpers added in Turn D.1."""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from guardian_pipeline import (
    analyze_iteration_intent,
    analyze_iteration_outcome,
    write_iteration_risk,
    write_fix_all_risk,
)
from ledger import ArtifactKind, Tier
from ledger_memory import InMemoryLedgerStore


def _run(coro):
    return asyncio.run(coro)


@dataclass
class _FakeResult:
    text: str
    model: str = "test"
    stop_reason: str = "end_turn"
    input_tokens: int = 50
    output_tokens: int = 25


class _RiskClient:
    def __init__(self, response_text):
        self.response_text = response_text
        self.last_prompt = ""

    async def complete(self, prompt, *, system=None, max_tokens=2048,
                       temperature=0.1, **kw):
        self.last_prompt = prompt
        return _FakeResult(text=self.response_text)


_DEFAULT_RISK_JSON = (
    '{"severity":"medium",'
    '"plain_narrative":"x","technical_narrative":"y",'
    '"affected_paths":["app/main.py"],'
    '"concerns":[{"path":"app/main.py","reason":"r","severity":"medium"}],'
    '"suggested_sequencing":[],"confidence":0.7}'
)


class TestAnalyzeIterationIntent(unittest.TestCase):
    def test_includes_all_three_path_categories_in_prompt(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        client = _RiskClient(_DEFAULT_RISK_JSON)

        _run(analyze_iteration_intent(
            store=store, project_id=pid,
            iteration_prompt="big change",
            planned_change_paths=["app/main.py"],
            planned_delete_paths=["old.py"],
            planned_new_paths=["new.py"],
            client=client,
        ))

        self.assertIn("Files to be regenerated", client.last_prompt)
        self.assertIn("app/main.py", client.last_prompt)
        self.assertIn("Files to be deleted", client.last_prompt)
        self.assertIn("old.py", client.last_prompt)
        self.assertIn("New files to be created", client.last_prompt)
        self.assertIn("new.py", client.last_prompt)
        # User prompt text is forwarded.
        self.assertIn("big change", client.last_prompt)

    def test_empty_plan_returns_low_severity_without_calling_model(self):
        """If the plan affects no files, we shouldn't waste an LLM call."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        client = _RiskClient(_DEFAULT_RISK_JSON)

        assessment = _run(analyze_iteration_intent(
            store=store, project_id=pid,
            iteration_prompt="no-op",
            planned_change_paths=[],
            planned_delete_paths=[],
            planned_new_paths=[],
            client=client,
        ))

        self.assertEqual(assessment.severity, "low")
        self.assertEqual(assessment.analyzer_model, "(skipped)")
        # No prompt was built.
        self.assertEqual(client.last_prompt, "")

    def test_single_file_target_is_just_the_path(self):
        """When exactly one file changes, target is just that path."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        client = _RiskClient(_DEFAULT_RISK_JSON)

        assessment = _run(analyze_iteration_intent(
            store=store, project_id=pid,
            iteration_prompt="one change",
            planned_change_paths=["app/main.py"],
            planned_delete_paths=[],
            planned_new_paths=[],
            client=client,
        ))
        self.assertEqual(assessment.target, "app/main.py")


class TestAnalyzeIterationOutcome(unittest.TestCase):
    def test_uses_actual_paths_not_planned(self):
        """The function takes actual_changed_paths etc. — distinct from
        the planner's predictions."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        client = _RiskClient(_DEFAULT_RISK_JSON)

        _run(analyze_iteration_outcome(
            store=store, project_id=pid,
            iteration_prompt="x",
            actual_changed_paths=["app/main.py"],
            actual_new_paths=[],
            actual_deleted_paths=[],
            client=client,
        ))

        self.assertIn("Files regenerated", client.last_prompt)
        self.assertIn("just completed", client.last_prompt)

    def test_no_changes_returns_low_severity_without_calling(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        client = _RiskClient(_DEFAULT_RISK_JSON)

        assessment = _run(analyze_iteration_outcome(
            store=store, project_id=pid,
            iteration_prompt="x",
            actual_changed_paths=[],
            actual_new_paths=[],
            actual_deleted_paths=[],
            client=client,
        ))
        self.assertEqual(assessment.severity, "low")
        self.assertEqual(assessment.analyzer_model, "(skipped)")
        self.assertEqual(client.last_prompt, "")


class TestWriteIterationRisk(unittest.TestCase):
    def test_writes_pre_risk_key(self):
        from guardian_pipeline import RiskAssessment
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        assessment = RiskAssessment(
            target="app/main.py", change_description="x",
            severity="medium", plain_narrative="p", technical_narrative="t",
            affected_paths=[], concerns=[], suggested_sequencing=[],
            confidence=0.7, analyzer_model="test",
            input_tokens=10, output_tokens=20, indexed_summary_count=0,
        )
        write_iteration_risk(store, pid, 1, "pre_risk", assessment)
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertIn("iteration:1:pre_risk", keys)

    def test_invalid_phase_rejected(self):
        from guardian_pipeline import RiskAssessment
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        assessment = RiskAssessment(
            target="x", change_description="x",
            severity="low", plain_narrative="", technical_narrative="",
            affected_paths=[], concerns=[], suggested_sequencing=[],
            confidence=0.5, analyzer_model="m",
            input_tokens=0, output_tokens=0, indexed_summary_count=0,
        )
        with self.assertRaises(ValueError):
            write_iteration_risk(store, pid, 1, "bogus_phase", assessment)


class TestWriteFixAllRisk(unittest.TestCase):
    def test_writes_pre_risk_key_for_fix_all(self):
        from guardian_pipeline import RiskAssessment
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        assessment = RiskAssessment(
            target="multi-file", change_description="fix-all",
            severity="high", plain_narrative="", technical_narrative="",
            affected_paths=["a.py", "b.py"], concerns=[],
            suggested_sequencing=[], confidence=0.6, analyzer_model="m",
            input_tokens=0, output_tokens=0, indexed_summary_count=0,
        )
        write_fix_all_risk(store, pid, 2, "post_risk", assessment)
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertIn("fix_all:2:post_risk", keys)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# list_iteration_risks / list_fix_all_risks — read back grouped records
# ---------------------------------------------------------------------------

class TestListIterationRisks(unittest.TestCase):
    def test_empty_project_returns_empty_dict(self):
        from guardian_pipeline import list_iteration_risks
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        self.assertEqual(list_iteration_risks(store, pid), {})

    def test_groups_pre_and_post_by_seq(self):
        from guardian_pipeline import (
            RiskAssessment, write_iteration_risk, list_iteration_risks,
        )
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        a = RiskAssessment(
            target="x", change_description="x",
            severity="medium", plain_narrative="", technical_narrative="",
            affected_paths=[], concerns=[], suggested_sequencing=[],
            confidence=0.7, analyzer_model="m",
            input_tokens=0, output_tokens=0, indexed_summary_count=0,
        )
        write_iteration_risk(store, pid, 1, "pre_risk", a)
        write_iteration_risk(store, pid, 1, "post_risk", a)
        write_iteration_risk(store, pid, 2, "pre_risk", a)

        result = list_iteration_risks(store, pid)
        self.assertIn(1, result)
        self.assertIn(2, result)
        self.assertIn("pre_risk", result[1])
        self.assertIn("post_risk", result[1])
        self.assertIn("pre_risk", result[2])
        self.assertNotIn("post_risk", result[2])

    def test_ignores_non_iteration_records(self):
        from guardian_pipeline import (
            RiskAssessment, write_iteration_risk, write_fix_all_risk,
            list_iteration_risks,
        )
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        a = RiskAssessment(
            target="x", change_description="x",
            severity="low", plain_narrative="", technical_narrative="",
            affected_paths=[], concerns=[], suggested_sequencing=[],
            confidence=0.5, analyzer_model="m",
            input_tokens=0, output_tokens=0, indexed_summary_count=0,
        )
        write_iteration_risk(store, pid, 1, "pre_risk", a)
        write_fix_all_risk(store, pid, 1, "pre_risk", a)

        result = list_iteration_risks(store, pid)
        # Only the iteration:1 record, not fix_all:1.
        self.assertEqual(set(result.keys()), {1})


class TestListFixAllRisks(unittest.TestCase):
    def test_groups_correctly(self):
        from guardian_pipeline import (
            RiskAssessment, write_fix_all_risk, list_fix_all_risks,
        )
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        a = RiskAssessment(
            target="x", change_description="x",
            severity="high", plain_narrative="", technical_narrative="",
            affected_paths=[], concerns=[], suggested_sequencing=[],
            confidence=0.6, analyzer_model="m",
            input_tokens=0, output_tokens=0, indexed_summary_count=0,
        )
        write_fix_all_risk(store, pid, 3, "pre_risk", a)
        write_fix_all_risk(store, pid, 3, "post_risk", a)

        result = list_fix_all_risks(store, pid)
        self.assertEqual(set(result.keys()), {3})
        self.assertIn("pre_risk", result[3])
        self.assertIn("post_risk", result[3])
