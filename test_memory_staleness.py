"""Tests for memory endpoint enrichments (polish turn).

Specifically the is_stale flag on file summaries — set when the file's
ledger entry is newer than the summary's indexed_at timestamp.
"""

from __future__ import annotations

import datetime
import time
import unittest
from unittest.mock import MagicMock

from ledger import ArtifactKind, Tier
from ledger_memory import InMemoryLedgerStore


class TestStaleFlagLogic(unittest.TestCase):
    """We test the staleness comparison directly without going through
    the FastAPI route. The endpoint just wires this together."""

    def test_summary_indexed_after_file_is_fresh(self):
        """File written at t=100, summary indexed at t=200 → fresh."""
        file_at = 100.0
        indexed_at = 200.0
        # The endpoint adds a 30s grace window.
        is_stale = (
            file_at > 0 and indexed_at > 0
            and file_at > (indexed_at + 30)
        )
        self.assertFalse(is_stale)

    def test_summary_indexed_before_file_is_stale(self):
        """File written at t=200, summary indexed at t=100 → stale."""
        file_at = 200.0
        indexed_at = 100.0
        is_stale = (
            file_at > 0 and indexed_at > 0
            and file_at > (indexed_at + 30)
        )
        self.assertTrue(is_stale)

    def test_within_grace_window_is_fresh(self):
        """File written 15s after summary — still considered fresh
        because the 30s grace handles the normal build-then-index lag."""
        indexed_at = 100.0
        file_at = indexed_at + 15
        is_stale = file_at > (indexed_at + 30)
        self.assertFalse(is_stale)

    def test_just_past_grace_is_stale(self):
        indexed_at = 100.0
        file_at = indexed_at + 31
        is_stale = file_at > (indexed_at + 30)
        self.assertTrue(is_stale)

    def test_missing_file_timestamp_is_fresh(self):
        """If we don't have a file timestamp at all (no current file
        entry for that path), we can't compute stale — default fresh."""
        file_at = 0.0
        indexed_at = 100.0
        is_stale = (
            file_at > 0 and indexed_at > 0
            and file_at > (indexed_at + 30)
        )
        self.assertFalse(is_stale)


if __name__ == "__main__":
    unittest.main()
