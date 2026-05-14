"""Tests for import_pipeline — URL parsing and pure helpers.

We don't test the full import flow (it requires actually cloning a
real repo, which would be flaky in CI). The URL parser and outcome
recording are the testable pieces; the clone+walk is tested manually
against real repos.
"""

from __future__ import annotations

import unittest

from import_pipeline import (
    parse_github_url,
    EXCLUDED_DIRS, EXCLUDED_EXTENSIONS,
    _is_excluded_extension, _is_under_excluded_dir,
    ImportOutcome, write_import_started, write_import_outcome,
)
from ledger import ArtifactKind
from ledger_memory import InMemoryLedgerStore
from pathlib import Path


class TestParseGithubUrl(unittest.TestCase):
    def test_full_https_url(self):
        owner, repo, https = parse_github_url(
            "https://github.com/anthropics/claude-code",
        )
        self.assertEqual(owner, "anthropics")
        self.assertEqual(repo, "claude-code")
        self.assertEqual(https, "https://github.com/anthropics/claude-code.git")

    def test_https_url_with_git_suffix(self):
        owner, repo, https = parse_github_url(
            "https://github.com/anthropics/claude-code.git",
        )
        self.assertEqual(owner, "anthropics")
        self.assertEqual(repo, "claude-code")
        # Normalized form still ends in .git.
        self.assertEqual(https, "https://github.com/anthropics/claude-code.git")

    def test_shorthand(self):
        owner, repo, https = parse_github_url("anthropics/claude-code")
        self.assertEqual(owner, "anthropics")
        self.assertEqual(repo, "claude-code")
        self.assertEqual(https, "https://github.com/anthropics/claude-code.git")

    def test_ssh_url(self):
        owner, repo, https = parse_github_url(
            "git@github.com:anthropics/claude-code.git",
        )
        self.assertEqual(owner, "anthropics")
        self.assertEqual(repo, "claude-code")
        # SSH URL is rewritten to HTTPS so we don't need SSH keys.
        self.assertEqual(https, "https://github.com/anthropics/claude-code.git")

    def test_url_with_extra_path(self):
        """A URL like .../tree/main/some/path should still parse the
        repo correctly — the extra path is ignored."""
        owner, repo, https = parse_github_url(
            "https://github.com/anthropics/claude-code/tree/main/src",
        )
        self.assertEqual(owner, "anthropics")
        self.assertEqual(repo, "claude-code")

    def test_non_github_host(self):
        """Should work with any git host, not just github.com."""
        owner, repo, https = parse_github_url(
            "https://gitlab.com/some-owner/some-repo",
        )
        self.assertEqual(owner, "some-owner")
        self.assertEqual(repo, "some-repo")
        self.assertEqual(https, "https://gitlab.com/some-owner/some-repo.git")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            parse_github_url("")

    def test_single_word_raises(self):
        with self.assertRaises(ValueError):
            parse_github_url("just-a-word")


class TestExclusionFilters(unittest.TestCase):
    def test_node_modules_excluded(self):
        self.assertTrue(
            _is_under_excluded_dir(Path("frontend/node_modules/foo.js")),
        )

    def test_root_node_modules_excluded(self):
        self.assertTrue(_is_under_excluded_dir(Path("node_modules/foo.js")))

    def test_normal_path_not_excluded(self):
        self.assertFalse(_is_under_excluded_dir(Path("src/app/main.py")))

    def test_dotgit_excluded(self):
        self.assertTrue(_is_under_excluded_dir(Path(".git/HEAD")))

    def test_image_extensions_excluded(self):
        for ext in (".png", ".jpg", ".gif", ".webp"):
            self.assertTrue(
                _is_excluded_extension(Path(f"x{ext}")), msg=ext,
            )

    def test_text_extensions_not_excluded(self):
        for ext in (".py", ".ts", ".md", ".sql", ".yaml"):
            self.assertFalse(
                _is_excluded_extension(Path(f"x{ext}")), msg=ext,
            )


class TestOutcomeRecording(unittest.TestCase):
    def test_write_import_started(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        write_import_started(store, pid, "https://github.com/x/y")
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertIn("import:started", keys)

    def test_write_import_outcome_success(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        outcome = ImportOutcome(
            files_imported=42,
            files_skipped_binary=3,
            total_bytes=12345,
            elapsed_seconds=1.5,
            repo_url="https://github.com/x/y",
            commit_sha="abcdef1234567890",
        )
        write_import_outcome(store, pid, outcome)
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertIn("import:outcome", keys)

    def test_write_import_outcome_failure(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        outcome = ImportOutcome(
            repo_url="bad url",
            error="Invalid URL: not parseable",
            elapsed_seconds=0.01,
        )
        write_import_outcome(store, pid, outcome)
        # Outcome still written even on failure — the UI needs to know.
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertIn("import:outcome", keys)


if __name__ == "__main__":
    unittest.main()
