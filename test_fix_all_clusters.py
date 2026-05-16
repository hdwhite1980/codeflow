"""Tests for fix-all A+B upgrade — clustering and memory-aware prompts.

Layer-by-layer:
  1. cluster_findings_by_topic: token-overlap grouping behavior
  2. build_cluster_prompt: structural correctness and memory injection
  3. discover_dependent_paths: graph traversal
"""

from __future__ import annotations

import unittest

from fix_all_pipeline import (
    Finding, FindingCluster,
    cluster_findings_by_topic,
    build_cluster_prompt,
    discover_dependent_paths,
)
from ledger import ArtifactKind, EdgeKind, Tier
from ledger_memory import InMemoryLedgerStore


def _finding(
    path: str, issue: str, *,
    severity: str = "medium",
    line: int = 1,
    suggestion: str = "",
    auditor: str = "openai",
) -> Finding:
    return Finding(
        file_path=path, issue=issue, severity=severity, line=line,
        suggestion=suggestion, auditor=auditor,
    )


class TestClusterFindingsByTopic(unittest.TestCase):
    def test_empty_findings_returns_empty(self):
        self.assertEqual(cluster_findings_by_topic([]), [])

    def test_same_topic_clusters_together(self):
        """Two findings about schema validation in two files should
        cluster, even though they're in different files."""
        findings = [
            _finding("app/store.js",
                     "Schema validation missing on entries",
                     severity="high"),
            _finding("app/suggestions.js",
                     "Schema validation missing on imports",
                     severity="high"),
        ]
        clusters = cluster_findings_by_topic(findings)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(len(clusters[0].findings), 2)
        self.assertEqual(len(clusters[0].file_paths), 2)

    def test_unrelated_findings_distinct_clusters(self):
        findings = [
            _finding("a.py", "Hard-coded credential token"),
            _finding("b.py", "Memory leak in pool allocator"),
            _finding("c.py", "Deprecated date format used"),
        ]
        clusters = cluster_findings_by_topic(findings)
        # Three findings with no token overlap → three clusters.
        self.assertEqual(len(clusters), 3)

    def test_critical_cluster_sorted_first(self):
        findings = [
            _finding("a.py", "Deprecated function used",
                     severity="low"),
            _finding("b.py", "Hard-coded API credential token in source",
                     severity="critical"),
            _finding("c.py", "Missing input validation on user fields",
                     severity="medium"),
        ]
        clusters = cluster_findings_by_topic(findings)
        # Critical-severity finding should be in the first cluster.
        worst_in_first = clusters[0].findings[0]
        self.assertEqual(worst_in_first.severity, "critical")

    def test_finding_with_empty_tokens_clusters_by_file(self):
        """Edge case: a finding with only stopwords in its text should
        still end up in some cluster (keyed by file path)."""
        findings = [_finding("a.py", "the is and")]  # all stopwords
        clusters = cluster_findings_by_topic(findings)
        self.assertEqual(len(clusters), 1)
        self.assertEqual(clusters[0].findings, findings)
        self.assertTrue(clusters[0].topic_key.startswith("misc:"))

    def test_partial_overlap_merges(self):
        """Findings with substantial token overlap but not identical
        should still cluster together."""
        findings = [
            _finding("a.py",
                     "Missing rate limiting on authentication endpoint",
                     severity="high"),
            _finding("b.py",
                     "Missing rate limiting on admin endpoint",
                     severity="high"),
        ]
        clusters = cluster_findings_by_topic(findings)
        # Should merge — overlap on rate/limiting/missing/endpoint is
        # well over 50%.
        self.assertEqual(len(clusters), 1)

    def test_max_clusters_caps_output(self):
        findings = [
            _finding(f"f{i}.py", f"distinct_issue_{i} term_{i}")
            for i in range(15)
        ]
        clusters = cluster_findings_by_topic(findings, max_clusters=5)
        self.assertLessEqual(len(clusters), 5)

    def test_topic_label_is_human_readable(self):
        findings = [
            _finding("a.py",
                     "Authentication bypass possible via header injection",
                     severity="critical"),
        ]
        clusters = cluster_findings_by_topic(findings)
        # Label should contain at least one meaningful word from the
        # issue text. We don't pin specific tokens because token order
        # in the label depends on extraction stability.
        label_lower = clusters[0].topic_label.lower()
        self.assertTrue(
            any(t in label_lower for t in
                ["authentication", "bypass", "header", "injection",
                 "possible", "via"]),
            f"label {clusters[0].topic_label!r} has no recognizable tokens",
        )


class TestBuildClusterPrompt(unittest.TestCase):
    def _basic_cluster(self) -> FindingCluster:
        findings = [
            _finding("a.py", "Schema validation missing", severity="high",
                     line=15, suggestion="Define entry schema"),
            _finding("a.py", "Schema validation missing", severity="high",
                     line=18, suggestion="Apply schema to elements"),
        ]
        return FindingCluster(
            topic_key="schema|validation|missing",
            topic_label="schema validation missing",
            findings=findings,
            file_paths=["a.py"],
        )

    def test_includes_finding_severities_and_lines(self):
        prompt = build_cluster_prompt(
            self._basic_cluster(),
            cluster_index=0, total_clusters=1,
        )
        self.assertIn("HIGH", prompt)
        self.assertIn("line 15", prompt)
        self.assertIn("line 18", prompt)
        self.assertIn("Define entry schema", prompt)

    def test_cluster_index_and_total_shown(self):
        prompt = build_cluster_prompt(
            self._basic_cluster(),
            cluster_index=2, total_clusters=5,
        )
        self.assertIn("cluster 3 of 5", prompt)

    def test_consistency_instruction_present(self):
        """The whole point — cluster prompts must tell the Builder
        to use a consistent approach across the cluster."""
        prompt = build_cluster_prompt(
            self._basic_cluster(),
            cluster_index=0, total_clusters=1,
        )
        self.assertIn("CONSISTENT", prompt)
        self.assertIn("right fix once", prompt.lower())

    def test_guardian_context_injected_when_provided(self):
        prompt = build_cluster_prompt(
            self._basic_cluster(),
            cluster_index=0, total_clusters=1,
            guardian_context="Project context: a.py manages the entry store...",
        )
        self.assertIn("Guardian semantic context", prompt)
        self.assertIn("manages the entry store", prompt)

    def test_no_guardian_section_when_empty(self):
        """Defensive: empty guardian context shouldn't render an empty
        section."""
        prompt = build_cluster_prompt(
            self._basic_cluster(),
            cluster_index=0, total_clusters=1,
            guardian_context="",
        )
        self.assertNotIn("Guardian semantic context", prompt)

    def test_dependent_paths_listed(self):
        prompt = build_cluster_prompt(
            self._basic_cluster(),
            cluster_index=0, total_clusters=1,
            dependent_paths=["b.py", "c.py"],
        )
        self.assertIn("depend on what you're changing", prompt)
        self.assertIn("b.py", prompt)
        self.assertIn("c.py", prompt)

    def test_safety_clause_present(self):
        """The "leave half-fixes alone" instruction must remain."""
        prompt = build_cluster_prompt(
            self._basic_cluster(),
            cluster_index=0, total_clusters=1,
        )
        # Either "leave that specific finding alone" or "do not produce
        # a half-fix" — both are the same idea.
        self.assertIn("half-fix", prompt.lower())

    def test_unfixable_instruction_present(self):
        """Cluster prompt must teach the Builder to use unfixable_findings
        for human-input cases, not invent values. This is the post-mortem
        fix for the Builder ignoring the system prompt's refusal path
        when the user prompt was pushing it to fix everything."""
        prompt = build_cluster_prompt(
            self._basic_cluster(),
            cluster_index=0, total_clusters=1,
        )
        self.assertIn("unfixable_findings", prompt)
        self.assertIn("decision_needed", prompt)
        # The cluster prompt should explicitly warn against inventing values.
        prompt_lower = prompt.lower()
        self.assertTrue(
            "invent" in prompt_lower or "fake values" in prompt_lower,
            "Cluster prompt should warn against inventing fake values",
        )


class TestDiscoverDependentPaths(unittest.TestCase):
    def test_no_targets_returns_empty(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        self.assertEqual(discover_dependent_paths(store, pid, []), [])

    def test_returns_empty_when_no_graph(self):
        """File exists but no edges in or out — no dependents."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        store.write_entry(
            project_id=pid, tier=Tier.GENERATION,
            artifact_kind=ArtifactKind.FILE,
            artifact_key=f"file:{pid}:a.py",
            body="content",
            rationale="t", author="t",
        )
        result = discover_dependent_paths(store, pid, ["a.py"])
        # InMemoryLedgerStore.neighbors() may not exist or may return
        # nothing — either way, no dependents.
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
