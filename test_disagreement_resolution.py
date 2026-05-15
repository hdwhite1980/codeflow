"""Tests for the disagreement resolution flow (Fix C UI).

Covers:
  - _disagreement_digest stability (same group → same digest)
  - write_audit_disagreements per-group key shape
  - list_audit_disagreements unresolved/all filtering
  - resolve_audit_disagreement actions: queue_fix, dismiss_both
  - loaded_resolved_disagreements_as_findings re-injection mechanics
"""

from __future__ import annotations

import time
import unittest

from fix_all_pipeline import (
    Finding,
    _disagreement_digest,
    write_audit_disagreements,
    list_audit_disagreements,
    resolve_audit_disagreement,
    loaded_resolved_disagreements_as_findings,
    write_fix_all_report,
)
from ledger import ArtifactKind
from ledger_memory import InMemoryLedgerStore


def _f(path: str, issue: str, *, severity: str = "warning",
       line: int = 1, suggestion: str = "", auditor: str = "openai") -> Finding:
    return Finding(
        file_path=path, severity=severity, line=line,
        issue=issue, suggestion=suggestion, auditor=auditor,
    )


class TestDisagreementDigest(unittest.TestCase):
    def test_stable_for_identical_group(self):
        g = [_f("a.py", "x", auditor="openai", line=10),
             _f("a.py", "y", auditor="gemini", line=10)]
        d1 = _disagreement_digest(g)
        d2 = _disagreement_digest(g)
        self.assertEqual(d1, d2)

    def test_order_independent(self):
        """Same findings in different order → same digest."""
        g1 = [_f("a.py", "x", auditor="openai", line=10),
              _f("a.py", "y", auditor="gemini", line=10)]
        g2 = [_f("a.py", "y", auditor="gemini", line=10),
              _f("a.py", "x", auditor="openai", line=10)]
        self.assertEqual(_disagreement_digest(g1), _disagreement_digest(g2))

    def test_different_file_different_digest(self):
        g1 = [_f("a.py", "x", auditor="openai", line=10)]
        g2 = [_f("b.py", "x", auditor="openai", line=10)]
        self.assertNotEqual(_disagreement_digest(g1), _disagreement_digest(g2))

    def test_paraphrased_suggestion_does_not_change_digest(self):
        """Issue text is the digest input, not suggestion. Same issue
        with different suggestion phrasings should produce the same
        digest so resolution survives audit re-runs."""
        g1 = [_f("a.py", "issue text",
                 auditor="openai", line=10, suggestion="A")]
        g2 = [_f("a.py", "issue text",
                 auditor="openai", line=10, suggestion="A but rephrased")]
        self.assertEqual(_disagreement_digest(g1), _disagreement_digest(g2))


class TestListUnresolved(unittest.TestCase):
    def test_writes_one_entry_per_group(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        groups = [
            [_f("a.py", "x", auditor="openai", line=10),
             _f("a.py", "y", auditor="gemini", line=10)],
            [_f("b.py", "x", auditor="openai", line=5),
             _f("b.py", "y", auditor="gemini", line=5)],
        ]
        write_audit_disagreements(store, pid, seq=1, disagreement_groups=groups)
        items = list_audit_disagreements(store, pid)
        self.assertEqual(len(items), 2)

    def test_includes_resolved_only_when_flag_set(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        group = [_f("a.py", "x", auditor="openai", line=10),
                 _f("a.py", "y", auditor="gemini", line=10)]
        write_audit_disagreements(store, pid, seq=1, disagreement_groups=[group])
        digest = _disagreement_digest(group)
        resolve_audit_disagreement(
            store, pid, digest, action="dismiss_both",
        )
        # Default list excludes resolved.
        self.assertEqual(list_audit_disagreements(store, pid), [])
        # With flag, included.
        all_items = list_audit_disagreements(
            store, pid, include_resolved=True,
        )
        self.assertEqual(len(all_items), 1)


class TestResolve(unittest.TestCase):
    def _setup_group(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        group = [_f("a.py", "x", auditor="openai", line=10, suggestion="A"),
                 _f("a.py", "y", auditor="gemini", line=10, suggestion="B")]
        write_audit_disagreements(store, pid, seq=1, disagreement_groups=[group])
        return store, pid, _disagreement_digest(group)

    def test_queue_fix_returns_chosen_finding(self):
        store, pid, digest = self._setup_group()
        chosen = resolve_audit_disagreement(
            store, pid, digest,
            action="queue_fix", chosen_auditor="openai",
        )
        self.assertIsNotNone(chosen)
        self.assertEqual(chosen["auditor"], "openai")
        self.assertEqual(chosen["suggestion"], "A")

    def test_dismiss_both_returns_none(self):
        store, pid, digest = self._setup_group()
        result = resolve_audit_disagreement(
            store, pid, digest, action="dismiss_both",
        )
        self.assertIsNone(result)

    def test_unknown_digest_returns_none(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        # No disagreements written.
        result = resolve_audit_disagreement(
            store, pid, "no-such-digest", action="dismiss_both",
        )
        self.assertIsNone(result)

    def test_queue_fix_requires_chosen_auditor(self):
        store, pid, digest = self._setup_group()
        with self.assertRaises(ValueError):
            resolve_audit_disagreement(
                store, pid, digest, action="queue_fix",
            )

    def test_invalid_action_raises(self):
        store, pid, digest = self._setup_group()
        with self.assertRaises(ValueError):
            resolve_audit_disagreement(
                store, pid, digest, action="not-a-real-action",
            )

    def test_queue_fix_for_unknown_auditor_raises(self):
        store, pid, digest = self._setup_group()
        with self.assertRaises(ValueError):
            resolve_audit_disagreement(
                store, pid, digest,
                action="queue_fix", chosen_auditor="not-an-auditor",
            )

    def test_resolved_record_preserved_on_redetect(self):
        """When fix-all re-detects the same disagreement (same digest)
        in a later pass, the prior resolution should be preserved."""
        store, pid, digest = self._setup_group()
        resolve_audit_disagreement(
            store, pid, digest,
            action="queue_fix", chosen_auditor="openai",
        )
        # Simulate a later fix-all pass re-detecting the same disagreement.
        group = [_f("a.py", "x", auditor="openai", line=10, suggestion="A"),
                 _f("a.py", "y", auditor="gemini", line=10, suggestion="B")]
        write_audit_disagreements(
            store, pid, seq=2, disagreement_groups=[group],
        )
        # The resolution from seq 1 should still be in effect.
        items = list_audit_disagreements(store, pid, include_resolved=True)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["resolved_action"], "queue_fix")
        self.assertEqual(items[0]["resolved_auditor"], "openai")


class TestReinjectionMechanics(unittest.TestCase):
    def test_no_resolutions_returns_empty(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        self.assertEqual(
            loaded_resolved_disagreements_as_findings(store, pid), [],
        )

    def test_returns_findings_after_resolution(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        group = [_f("a.py", "x", auditor="openai", line=10, suggestion="A"),
                 _f("a.py", "y", auditor="gemini", line=10, suggestion="B")]
        write_audit_disagreements(store, pid, seq=1, disagreement_groups=[group])
        digest = _disagreement_digest(group)
        resolve_audit_disagreement(
            store, pid, digest,
            action="queue_fix", chosen_auditor="openai",
        )
        findings = loaded_resolved_disagreements_as_findings(store, pid)
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].auditor, "openai")
        self.assertEqual(findings[0].suggestion, "A")

    def test_dismiss_both_does_not_inject(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        group = [_f("a.py", "x", auditor="openai", line=10),
                 _f("a.py", "y", auditor="gemini", line=10)]
        write_audit_disagreements(store, pid, seq=1, disagreement_groups=[group])
        digest = _disagreement_digest(group)
        resolve_audit_disagreement(
            store, pid, digest, action="dismiss_both",
        )
        # No re-injection.
        self.assertEqual(
            loaded_resolved_disagreements_as_findings(store, pid), [],
        )

    def test_resolution_predating_fix_all_pass_not_re_injected(self):
        """Once a resolution has 'had its chance' (predates the most
        recent fix-all completion), it doesn't keep getting re-injected.
        Prevents infinite re-queue of the same finding."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        group = [_f("a.py", "x", auditor="openai", line=10, suggestion="A"),
                 _f("a.py", "y", auditor="gemini", line=10, suggestion="B")]
        write_audit_disagreements(store, pid, seq=1, disagreement_groups=[group])
        digest = _disagreement_digest(group)
        # Resolve at time T.
        resolve_audit_disagreement(
            store, pid, digest,
            action="queue_fix", chosen_auditor="openai",
        )
        # Simulate fix-all completing AFTER the resolution.
        time.sleep(0.01)  # ensure timestamp ordering
        write_fix_all_report(
            store, pid, seq=2, report="test", pre_count=1, post_count=0,
            fixed=1, regressions=0,
        )
        # Now the resolution is older than the fix-all report — already
        # had its shot. Re-injection should be empty.
        self.assertEqual(
            loaded_resolved_disagreements_as_findings(store, pid), [],
        )


if __name__ == "__main__":
    unittest.main()
