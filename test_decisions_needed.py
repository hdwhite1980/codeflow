"""Tests for the decisions-needed flow (Fix D + Fix F).

Covers:
  - _decision_digest stability
  - write_decisions_needed persistence + per-digest keying
  - list_decisions_needed sorting + filtering
  - resolve_decision_needed actions
  - loaded_resolved_decisions_as_findings re-injection mechanics
  - suppress_unfixable_from_regressions filter behavior
"""

from __future__ import annotations

import time
import unittest

from fix_all_pipeline import (
    Finding,
    _decision_digest,
    write_decisions_needed,
    list_decisions_needed,
    resolve_decision_needed,
    loaded_resolved_decisions_as_findings,
    suppress_unfixable_from_regressions,
    write_fix_all_report,
)
from ledger import ArtifactKind
from ledger_memory import InMemoryLedgerStore


def _u(file_path: str, issue: str, *,
       line: int = None, decision_needed: str = "What value?",
       decision_type: str = "value",
       blocking_info: str = "") -> dict:
    return {
        "file_path": file_path,
        "line": line,
        "issue": issue,
        "decision_needed": decision_needed,
        "decision_type": decision_type,
        "blocking_info": blocking_info,
    }


def _f(path: str, issue: str, *, severity: str = "warning",
       line: int = 1, suggestion: str = "", auditor: str = "openai") -> Finding:
    return Finding(
        file_path=path, severity=severity, line=line,
        issue=issue, suggestion=suggestion, auditor=auditor,
    )


class TestDecisionDigest(unittest.TestCase):
    def test_stable(self):
        d1 = _decision_digest("a.psd1", "RequiredModules is empty")
        d2 = _decision_digest("a.psd1", "RequiredModules is empty")
        self.assertEqual(d1, d2)

    def test_different_files_distinct(self):
        d1 = _decision_digest("a.psd1", "RequiredModules is empty")
        d2 = _decision_digest("b.psd1", "RequiredModules is empty")
        self.assertNotEqual(d1, d2)


class TestPersistAndList(unittest.TestCase):
    def test_writes_per_finding(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[
            _u("a.psd1", "RequiredModules empty"),
            _u("a.psd1", "PowerShellVersion contradicts CompatiblePSEditions"),
        ])
        items = list_decisions_needed(store, pid)
        self.assertEqual(len(items), 2)

    def test_skips_invalid_entries(self):
        """Malformed entries shouldn't blow up — they get dropped."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[
            _u("a.psd1", "valid"),
            {"file_path": "", "issue": "missing path"},  # invalid
            {"file_path": "b.psd1"},  # missing issue
            "not even a dict",  # invalid
        ])
        items = list_decisions_needed(store, pid)
        self.assertEqual(len(items), 1)

    def test_empty_list_no_op(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[])
        items = list_decisions_needed(store, pid, include_resolved=True)
        self.assertEqual(items, [])

    def test_sort_architectural_first(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[
            _u("a.py", "policy q", decision_type="policy"),
            _u("a.py", "architectural q", decision_type="architectural"),
            _u("a.py", "value q", decision_type="value"),
        ])
        items = list_decisions_needed(store, pid)
        self.assertEqual(items[0]["decision_type"], "architectural")

    def test_unresolved_only_by_default(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[
            _u("a.psd1", "test"),
        ])
        digest = _decision_digest("a.psd1", "test")
        resolve_decision_needed(
            store, pid, digest, action="dismiss",
        )
        # Default excludes resolved.
        self.assertEqual(list_decisions_needed(store, pid), [])
        # With flag, included.
        self.assertEqual(
            len(list_decisions_needed(store, pid, include_resolved=True)),
            1,
        )

    def test_resolved_state_preserved_on_redetect(self):
        """Builder re-emits the same refusal next pass; resolution
        should persist."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[
            _u("a.psd1", "test issue"),
        ])
        digest = _decision_digest("a.psd1", "test issue")
        resolve_decision_needed(
            store, pid, digest,
            action="provide_value", value="https://example.com",
        )
        # Builder re-emits in pass 2.
        write_decisions_needed(store, pid, seq=2, unfixable_findings=[
            _u("a.psd1", "test issue"),
        ])
        # Resolution from pass 1 still in effect.
        items = list_decisions_needed(store, pid, include_resolved=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["resolved_action"], "provide_value")
        self.assertEqual(items[0]["resolved_value"], "https://example.com")


class TestResolve(unittest.TestCase):
    def _setup(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[
            _u("a.psd1", "test issue", decision_needed="What URL?"),
        ])
        return store, pid, _decision_digest("a.psd1", "test issue")

    def test_provide_value(self):
        store, pid, digest = self._setup()
        result = resolve_decision_needed(
            store, pid, digest,
            action="provide_value", value="https://example.com/license",
        )
        self.assertIsNotNone(result)
        self.assertEqual(result["resolved_value"], "https://example.com/license")

    def test_dismiss_returns_none(self):
        store, pid, digest = self._setup()
        result = resolve_decision_needed(
            store, pid, digest, action="dismiss",
        )
        self.assertIsNone(result)

    def test_unknown_digest(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        result = resolve_decision_needed(
            store, pid, "nonexistent", action="dismiss",
        )
        self.assertIsNone(result)

    def test_provide_value_requires_nonempty_value(self):
        store, pid, digest = self._setup()
        with self.assertRaises(ValueError):
            resolve_decision_needed(
                store, pid, digest, action="provide_value", value="",
            )
        with self.assertRaises(ValueError):
            resolve_decision_needed(
                store, pid, digest, action="provide_value", value="   ",
            )

    def test_invalid_action(self):
        store, pid, digest = self._setup()
        with self.assertRaises(ValueError):
            resolve_decision_needed(
                store, pid, digest, action="not-an-action",
            )


class TestReinjection(unittest.TestCase):
    def test_provide_value_produces_finding_with_user_value(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[
            _u("a.psd1", "RequiredModules empty",
               line=31,
               decision_needed="Mandatory or optional?"),
        ])
        digest = _decision_digest("a.psd1", "RequiredModules empty")
        resolve_decision_needed(
            store, pid, digest,
            action="provide_value",
            value="optional - declare in PSData ExternalModuleDependencies",
        )
        findings = loaded_resolved_decisions_as_findings(store, pid)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].file_path, "a.psd1")
        # User's value should appear in the suggestion so the Builder
        # knows what to write.
        self.assertIn("optional", findings[0].suggestion)
        self.assertIn("ExternalModuleDependencies", findings[0].suggestion)
        self.assertEqual(findings[0].auditor, "user:decision")

    def test_dismiss_does_not_inject(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[
            _u("a.psd1", "URL placeholder"),
        ])
        digest = _decision_digest("a.psd1", "URL placeholder")
        resolve_decision_needed(store, pid, digest, action="dismiss")
        self.assertEqual(
            loaded_resolved_decisions_as_findings(store, pid), [],
        )

    def test_resolution_predating_fix_all_not_re_injected(self):
        """Once a fix-all pass has completed AFTER the resolution, the
        finding has had its chance to run and shouldn't be re-injected."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_decisions_needed(store, pid, seq=1, unfixable_findings=[
            _u("a.psd1", "test"),
        ])
        digest = _decision_digest("a.psd1", "test")
        resolve_decision_needed(
            store, pid, digest,
            action="provide_value", value="my-value",
        )
        time.sleep(0.01)
        write_fix_all_report(
            store, pid, seq=2, report="test",
            pre_count=1, post_count=0, fixed=1, regressions=0,
        )
        # Resolution is older than the fix-all report → already had its
        # chance. Not re-injected.
        self.assertEqual(
            loaded_resolved_decisions_as_findings(store, pid), [],
        )


class TestSuppressUnfixableFromRegressions(unittest.TestCase):
    def test_empty_records_passes_all_through(self):
        findings = [_f("a.py", "real bug")]
        kept, suppressed = suppress_unfixable_from_regressions(findings, [])
        self.assertEqual(len(kept), 1)
        self.assertEqual(suppressed, [])

    def test_matching_post_finding_suppressed(self):
        unfixable = [{
            "file_path": "a.psd1",
            "issue": "RequiredModules is empty needs dependency declaration",
        }]
        post = [_f("a.psd1", "RequiredModules empty requires dependency")]
        kept, suppressed = suppress_unfixable_from_regressions(post, unfixable)
        self.assertEqual(len(kept), 0)
        self.assertEqual(len(suppressed), 1)

    def test_different_file_not_suppressed(self):
        unfixable = [{
            "file_path": "a.psd1",
            "issue": "RequiredModules empty",
        }]
        post = [_f("b.psd1", "RequiredModules empty")]
        kept, _ = suppress_unfixable_from_regressions(post, unfixable)
        self.assertEqual(len(kept), 1)

    def test_low_similarity_not_suppressed(self):
        unfixable = [{
            "file_path": "a.py",
            "issue": "Hard-coded credential token in source",
        }]
        post = [_f("a.py", "Memory leak in allocator pool resource")]
        kept, _ = suppress_unfixable_from_regressions(post, unfixable)
        self.assertEqual(len(kept), 1)


if __name__ == "__main__":
    unittest.main()
