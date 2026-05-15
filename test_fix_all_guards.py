"""Tests for the fix-all anti-oscillation guards.

Three independent features, one test file:
  - Fix A: audit pipeline switches to docs-narrow prompt for documentation files
  - Fix B: filter_oscillating_findings drops findings that match recently-resolved
  - Fix C: detect_auditor_disagreements pulls contradictory pairs out of fix-all

These compose: A reduces volume on doc files, B catches same-auditor flip-flops,
C catches cross-auditor contradictions.
"""

from __future__ import annotations

import time
import unittest

from audit_pipeline import _audit_user_prompt, _is_documentation_file
from fix_all_pipeline import (
    Finding,
    filter_oscillating_findings,
    detect_auditor_disagreements,
    load_recent_resolved_findings,
    write_fix_all_resolved,
    write_audit_disagreements,
)
from ledger_memory import InMemoryLedgerStore


def _f(path: str, issue: str, *, severity: str = "warning",
       line: int = 1, suggestion: str = "", auditor: str = "openai") -> Finding:
    return Finding(
        file_path=path, severity=severity, line=line,
        issue=issue, suggestion=suggestion, auditor=auditor,
    )


# ---------------------------------------------------------------------------
# Fix A: docs-narrow audit prompt
# ---------------------------------------------------------------------------

class TestIsDocumentationFile(unittest.TestCase):
    def test_common_doc_extensions(self):
        for path in ["README.md", "API.markdown", "guide.rst",
                     "notes.txt", "intro.adoc"]:
            self.assertTrue(_is_documentation_file(path),
                            f"{path} should be doc")

    def test_extensionless_doc_basenames(self):
        for path in ["README", "LICENSE", "CHANGELOG",
                     "CONTRIBUTING", "AUTHORS"]:
            self.assertTrue(_is_documentation_file(path))

    def test_case_insensitive(self):
        self.assertTrue(_is_documentation_file("readme.MD"))
        self.assertTrue(_is_documentation_file("Readme"))
        self.assertTrue(_is_documentation_file("LICENSE.TXT"))

    def test_docs_directory(self):
        self.assertTrue(_is_documentation_file("docs/api.md"))
        self.assertTrue(_is_documentation_file("docs/internal/guide.rst"))
        self.assertTrue(_is_documentation_file("project/doc/setup"))

    def test_code_files_not_docs(self):
        for path in ["app.py", "src/main.ts", "manifest.psd1",
                     "test.py", "Makefile", "package.json"]:
            self.assertFalse(_is_documentation_file(path),
                             f"{path} should NOT be doc")

    def test_empty_string(self):
        self.assertFalse(_is_documentation_file(""))


class TestAuditPromptBranching(unittest.TestCase):
    def test_doc_prompt_for_markdown(self):
        p = _audit_user_prompt(
            file_path="README.md", file_content="# Test",
            purpose="docs", language="markdown",
        )
        # Doc prompt has the narrow ruleset.
        self.assertIn("DOCUMENTATION", p)
        self.assertIn("Style, phrasing", p)
        # Doc prompt should NOT include the generic checklist.
        self.assertNotIn("Is the file complete?", p)

    def test_code_prompt_unchanged(self):
        p = _audit_user_prompt(
            file_path="app.py", file_content="print(1)",
            purpose="entry", language="python",
        )
        # Code prompt keeps the generic checklist intact.
        self.assertIn("each category in order", p)
        self.assertNotIn("DOCUMENTATION", p)

    def test_docs_dir_uses_narrow_prompt(self):
        """A .py file in docs/ would be ambiguous; we cover the
        common case (md in docs/)."""
        p = _audit_user_prompt(
            file_path="docs/api.md", file_content="# API",
            purpose="docs", language="markdown",
        )
        self.assertIn("DOCUMENTATION", p)

    def test_doc_prompt_tells_auditor_empty_is_ok(self):
        """Critical for stopping the loop: the auditor must know that
        finding nothing is the expected outcome."""
        p = _audit_user_prompt(
            file_path="README.md", file_content="# Test",
            purpose="docs", language="markdown",
        )
        self.assertIn("empty findings list", p.lower())
        self.assertIn("expected outcome", p.lower())


# ---------------------------------------------------------------------------
# Fix B: oscillation filter
# ---------------------------------------------------------------------------

class TestFilterOscillatingFindings(unittest.TestCase):
    def test_no_history_keeps_everything(self):
        findings = [_f("a.py", "Bug found")]
        kept, suppressed = filter_oscillating_findings(findings, [])
        self.assertEqual(kept, findings)
        self.assertEqual(suppressed, [])

    def test_drops_finding_matching_recent_resolved(self):
        """Same file, very similar issue text → suppressed.

        In production the oscillating findings tend to be near-
        duplicates of each other (the auditor flips its mind about
        the same observation). The threshold defaults to 0.6 — these
        share 'remove', 'cross-platform', 'claim', 'PowerShell',
        'required', producing ~0.7 Jaccard."""
        recent = [{
            "file_path": "README.md",
            "issue": "PowerShell 5.1 required; remove cross-platform claim",
            "line": 22,
        }]
        findings = [_f("README.md",
                       "PowerShell 5.1 required; remove cross-platform claim from intro")]
        kept, suppressed = filter_oscillating_findings(findings, recent)
        self.assertEqual(len(kept), 0)
        self.assertEqual(len(suppressed), 1)

    def test_partial_overlap_above_threshold_dropped(self):
        """At default threshold 0.45, this overlap (~0.43) sits just
        below the cutoff. Specifically dial threshold lower to confirm
        the gradient works."""
        recent = [{
            "file_path": "README.md",
            "issue": "PowerShell 5.1 required; remove cross-platform claim",
            "line": 22,
        }]
        findings = [_f("README.md",
                       "PowerShell cross-platform claim incorrect; require 5.1")]
        kept, _ = filter_oscillating_findings(
            findings, recent, similarity_threshold=0.4,
        )
        self.assertEqual(len(kept), 0)

    def test_overlap_well_below_threshold_kept(self):
        """Genuinely different findings should always pass through
        regardless of threshold tuning."""
        recent = [{
            "file_path": "app.py",
            "issue": "Hardcoded API key in source",
        }]
        findings = [_f("app.py", "Missing input validation on POST endpoint")]
        kept, _ = filter_oscillating_findings(findings, recent)
        self.assertEqual(len(kept), 1)

    def test_keeps_finding_when_file_differs(self):
        """File scope: a similar issue on a DIFFERENT file is not
        oscillation, it's a parallel finding."""
        recent = [{
            "file_path": "README.md",
            "issue": "PowerShell version mismatch claim",
        }]
        findings = [_f("setup.md",
                       "PowerShell version mismatch claim")]
        kept, suppressed = filter_oscillating_findings(findings, recent)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(suppressed), 0)

    def test_keeps_finding_when_text_differs(self):
        """Same file, totally different issue → keep."""
        recent = [{
            "file_path": "app.py",
            "issue": "Hardcoded API key in source",
        }]
        findings = [_f("app.py", "Missing error handler in main loop")]
        kept, suppressed = filter_oscillating_findings(findings, recent)
        self.assertEqual(len(kept), 1)

    def test_threshold_configurable(self):
        """Caller can dial sensitivity. With threshold=0.99 even very
        similar text shouldn't suppress."""
        recent = [{
            "file_path": "a.py",
            "issue": "Schema validation missing on entries",
        }]
        findings = [_f("a.py", "Schema validation missing on imports")]
        kept, _ = filter_oscillating_findings(
            findings, recent, similarity_threshold=0.99,
        )
        self.assertEqual(len(kept), 1)


class TestPersistResolved(unittest.TestCase):
    def test_round_trip(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        resolved = [
            _f("a.py", "issue 1"),
            _f("b.py", "issue 2"),
        ]
        write_fix_all_resolved(store, pid, seq=3, resolved_findings=resolved)
        loaded = load_recent_resolved_findings(store, pid)
        self.assertEqual(len(loaded), 2)
        paths = {e["file_path"] for e in loaded}
        self.assertEqual(paths, {"a.py", "b.py"})

    def test_lookback_respected(self):
        """Only the most recent N passes' resolved-findings are loaded."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        for seq in range(1, 6):
            write_fix_all_resolved(
                store, pid, seq=seq,
                resolved_findings=[_f(f"f{seq}.py", f"issue {seq}")],
            )
        loaded = load_recent_resolved_findings(
            store, pid, lookback_passes=2,
        )
        # Most recent two passes: seq 4 and seq 5.
        files = {e["file_path"] for e in loaded}
        self.assertEqual(files, {"f4.py", "f5.py"})


# ---------------------------------------------------------------------------
# Fix C: auditor disagreement detection
# ---------------------------------------------------------------------------

class TestDetectAuditorDisagreements(unittest.TestCase):
    def test_no_findings_returns_empty(self):
        consensus, groups = detect_auditor_disagreements([])
        self.assertEqual(consensus, [])
        self.assertEqual(groups, [])

    def test_single_finding_is_consensus(self):
        findings = [_f("a.py", "bug", auditor="openai")]
        consensus, groups = detect_auditor_disagreements(findings)
        self.assertEqual(len(consensus), 1)
        self.assertEqual(groups, [])

    def test_same_auditor_duplicates_passthrough(self):
        """Both findings from the SAME auditor — not a disagreement."""
        findings = [
            _f("a.py", "x", line=10, suggestion="add A", auditor="openai"),
            _f("a.py", "y", line=11, suggestion="remove A", auditor="openai"),
        ]
        consensus, groups = detect_auditor_disagreements(findings)
        self.assertEqual(len(consensus), 2)
        self.assertEqual(groups, [])

    def test_different_auditors_same_line_contradictory(self):
        """Classic disagreement: same file, near same line, different
        auditors, contradictory suggestions."""
        findings = [
            _f("README.md", "PS 7 not supported",
               line=22,
               suggestion="remove cross-platform claim require Windows PowerShell 5.1",
               auditor="openai"),
            _f("README.md", "PS 7 is fully supported",
               line=22,
               suggestion="add cross-platform support note for PowerShell 7+",
               auditor="gemini"),
        ]
        consensus, groups = detect_auditor_disagreements(findings)
        # Neither should appear in consensus; one disagreement group.
        self.assertEqual(len(consensus), 0)
        self.assertEqual(len(groups), 1)
        self.assertEqual(len(groups[0]), 2)
        auditors_in_group = {f.auditor for f in groups[0]}
        self.assertEqual(auditors_in_group, {"openai", "gemini"})

    def test_different_auditors_aligned_suggestions_are_consensus(self):
        """Both auditors agree on the fix — not a disagreement."""
        findings = [
            _f("a.py", "missing rate limit",
               line=5, suggestion="add token bucket rate limiter middleware",
               auditor="openai"),
            _f("a.py", "rate limiter absent",
               line=5, suggestion="add token bucket rate limiter middleware",
               auditor="gemini"),
        ]
        consensus, groups = detect_auditor_disagreements(findings)
        # Both flagged the same thing with the same fix → real agreement.
        # Both should be kept (the de-dup happens elsewhere in the pipeline).
        self.assertEqual(len(consensus), 2)
        self.assertEqual(groups, [])

    def test_line_window_groups_nearby_lines(self):
        """Findings on adjacent lines from different auditors with
        contradictory suggestions should still be detected as one
        disagreement (auditors don't always pick the same line)."""
        findings = [
            _f("README.md", "claim X is wrong",
               line=22, suggestion="remove claim X",
               auditor="openai"),
            _f("README.md", "claim X is right",
               line=24, suggestion="strengthen claim X with examples",
               auditor="gemini"),
        ]
        consensus, groups = detect_auditor_disagreements(findings)
        self.assertEqual(len(groups), 1)


class TestWriteDisagreements(unittest.TestCase):
    def test_empty_groups_no_write(self):
        """Don't clutter the ledger with empty markers."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        write_audit_disagreements(store, pid, seq=1, disagreement_groups=[])
        # No ledger entry for fix_all:1:disagreements should exist.
        from ledger import ArtifactKind
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        markers = [d for d in decisions
                   if d.artifact_key.endswith(":disagreements")]
        self.assertEqual(markers, [])

    def test_writes_when_groups_present(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        group = [
            _f("a.md", "x", auditor="openai", suggestion="remove"),
            _f("a.md", "y", auditor="gemini", suggestion="add more"),
        ]
        write_audit_disagreements(
            store, pid, seq=1, disagreement_groups=[group],
        )
        from ledger import ArtifactKind
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        markers = [d for d in decisions
                   if d.artifact_key.endswith(":disagreements")]
        self.assertEqual(len(markers), 1)


if __name__ == "__main__":
    unittest.main()
