"""Tests for fix_all_pipeline.

Covers the pure functions (collect_all_findings, build_fix_all_prompt,
estimate_fix_all_cost, _finding_key) and the marker writers. The
end-to-end handle_fix_all flow needs a live Anthropic client and is
exercised in production smoke tests.
"""

from __future__ import annotations

import json
import unittest

from fix_all_pipeline import (
    FIX_ALL_MAX_FINDINGS,
    Finding,
    build_fix_all_prompt,
    collect_all_findings,
    estimate_fix_all_cost,
    next_fix_all_seq,
    write_fix_all_report,
    write_fix_all_started,
    _finding_key,
)
from ledger import ArtifactKind, Tier
from ledger_memory import InMemoryLedgerStore


def _seed_verdict(store, pid, path, auditor, findings):
    """Write an audit_verdict entry with the given findings list."""
    store.write_entry(
        project_id=pid, tier=Tier.AUDIT,
        artifact_kind=ArtifactKind.AUDIT_VERDICT,
        artifact_key=f"ref:{pid}:audit:{auditor}:{path}",
        body={"file_path": path, "findings": findings},
        rationale=f"{auditor} audit of {path}: {len(findings)} findings",
        author=f"{auditor}:audit",
    )


class TestCollectAllFindings(unittest.TestCase):
    def test_empty_project_returns_empty_list(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        findings, truncated = collect_all_findings(store, pid)
        self.assertEqual(findings, [])
        self.assertFalse(truncated)

    def test_collects_all_severities_by_default(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _seed_verdict(store, pid, "a.py", "openai", [
            {"severity": "critical", "line": 1, "issue": "crash on null"},
            {"severity": "warning", "line": 2, "issue": "performance"},
            {"severity": "nit", "line": 3, "issue": "style"},
        ])
        findings, _ = collect_all_findings(store, pid)
        self.assertEqual(len(findings), 3)
        # Severity-first ordering: critical → warning → nit.
        self.assertEqual(findings[0].severity, "critical")
        self.assertEqual(findings[1].severity, "warning")
        self.assertEqual(findings[2].severity, "nit")

    def test_severity_filter_excludes_others(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _seed_verdict(store, pid, "a.py", "openai", [
            {"severity": "critical", "line": 1, "issue": "x"},
            {"severity": "nit", "line": 2, "issue": "y"},
        ])
        findings, _ = collect_all_findings(
            store, pid, severities=("critical",),
        )
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].severity, "critical")

    def test_truncation_at_cap(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        many = [
            {"severity": "warning", "line": i, "issue": f"x{i}"}
            for i in range(10)
        ]
        _seed_verdict(store, pid, "a.py", "openai", many)
        findings, truncated = collect_all_findings(
            store, pid, max_findings=5,
        )
        self.assertEqual(len(findings), 5)
        self.assertTrue(truncated)

    def test_handles_missing_fields_gracefully(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _seed_verdict(store, pid, "a.py", "openai", [
            {"severity": "warning"},  # no line, no issue
            {"severity": "critical", "issue": "crash"},  # no line
        ])
        findings, _ = collect_all_findings(store, pid)
        self.assertEqual(len(findings), 2)
        # Missing fields don't crash; line becomes None, issue becomes "".
        self.assertIsNone(findings[0].line)

    def test_collects_from_multiple_auditors(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _seed_verdict(store, pid, "a.py", "openai", [
            {"severity": "critical", "line": 1, "issue": "openai found this"},
        ])
        _seed_verdict(store, pid, "a.py", "gemini", [
            {"severity": "warning", "line": 5, "issue": "gemini found that"},
        ])
        findings, _ = collect_all_findings(store, pid)
        self.assertEqual(len(findings), 2)
        auditors = {f.auditor for f in findings}
        self.assertEqual(auditors, {"openai", "gemini"})


class TestBuildPrompt(unittest.TestCase):
    def test_groups_findings_by_file(self):
        findings = [
            Finding("a.py", "critical", 1, "issue A1", "fix A1", "openai"),
            Finding("a.py", "warning", 2, "issue A2", "", "openai"),
            Finding("b.py", "nit", 3, "issue B1", "fix B1", "gemini"),
        ]
        prompt = build_fix_all_prompt(findings, truncated=False)
        # Both files appear as headers.
        self.assertIn("## a.py", prompt)
        self.assertIn("## b.py", prompt)
        # Severities present.
        self.assertIn("[CRITICAL]", prompt)
        self.assertIn("[WARNING]", prompt)
        self.assertIn("[NIT]", prompt)
        # Suggestions included when present.
        self.assertIn("Suggested fix: fix A1", prompt)
        # The "skip if can't fix" instruction is there.
        self.assertIn("cannot be safely fixed", prompt)

    def test_truncated_note_appended(self):
        findings = [Finding("a.py", "critical", 1, "x", "", "openai")]
        prompt = build_fix_all_prompt(findings, truncated=True)
        self.assertIn("more than", prompt.lower())


class TestEstimate(unittest.TestCase):
    def test_zero_findings_zero_cost(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        est = estimate_fix_all_cost(store, pid)
        self.assertEqual(est["issues_to_fix"], 0)
        self.assertEqual(est["files_affected"], 0)
        self.assertEqual(est["estimated_cost_usd_low"], 0.0)

    def test_estimate_scales_with_files(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _seed_verdict(store, pid, "a.py", "openai", [
            {"severity": "warning", "line": 1, "issue": "x"},
        ])
        _seed_verdict(store, pid, "b.py", "openai", [
            {"severity": "warning", "line": 1, "issue": "y"},
        ])
        _seed_verdict(store, pid, "c.py", "openai", [
            {"severity": "warning", "line": 1, "issue": "z"},
        ])
        est = estimate_fix_all_cost(store, pid)
        self.assertEqual(est["issues_to_fix"], 3)
        self.assertEqual(est["files_affected"], 3)
        # High estimate strictly > low; both positive.
        self.assertGreater(est["estimated_cost_usd_high"], est["estimated_cost_usd_low"])
        self.assertGreater(est["estimated_cost_usd_low"], 0.0)

    def test_by_severity_breakdown(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _seed_verdict(store, pid, "a.py", "openai", [
            {"severity": "critical", "line": 1, "issue": "x"},
            {"severity": "critical", "line": 2, "issue": "y"},
            {"severity": "warning", "line": 3, "issue": "z"},
            {"severity": "nit", "line": 4, "issue": "w"},
        ])
        est = estimate_fix_all_cost(store, pid)
        self.assertEqual(est["by_severity"]["critical"], 2)
        self.assertEqual(est["by_severity"]["warning"], 1)
        self.assertEqual(est["by_severity"]["nit"], 1)


class TestFindingKey(unittest.TestCase):
    """Pre/post comparison depends on _finding_key being stable across
    minor wording variations. Test that auditor name is NOT part of
    the key (so if a finding moves from one auditor to another, it
    still counts as 'persists')."""

    def test_same_finding_different_auditor_same_key(self):
        a = Finding("x.py", "critical", 10, "crashes on null", "", "openai")
        b = Finding("x.py", "critical", 10, "crashes on null", "", "gemini")
        self.assertEqual(_finding_key(a), _finding_key(b))

    def test_different_line_different_key(self):
        a = Finding("x.py", "critical", 10, "issue", "", "openai")
        b = Finding("x.py", "critical", 11, "issue", "", "openai")
        self.assertNotEqual(_finding_key(a), _finding_key(b))


class TestMarkerWriters(unittest.TestCase):
    def test_write_started_then_report(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        write_fix_all_started(store, pid, seq=1, issue_count=5, files_affected=3)
        write_fix_all_report(
            store, pid, seq=1, report="Fixed 3 of 5",
            pre_count=5, post_count=2, fixed=3, regressions=0,
        )
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertIn("fix_all:1:started", keys)
        self.assertIn("fix_all:1:report", keys)

    def test_next_seq_counts_started_markers(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        self.assertEqual(next_fix_all_seq(store, pid), 1)
        write_fix_all_started(store, pid, 1, 5, 3)
        self.assertEqual(next_fix_all_seq(store, pid), 2)
        write_fix_all_started(store, pid, 2, 1, 1)
        self.assertEqual(next_fix_all_seq(store, pid), 3)


if __name__ == "__main__":
    unittest.main()
