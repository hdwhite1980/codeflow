"""Tests for stop_flag (cooperative cancellation) and project deletion."""

from __future__ import annotations

import asyncio
import unittest

from ledger_memory import InMemoryLedgerStore
from ledger import ArtifactKind, Tier
from stop_flag import (
    InMemoryStopFlag, make_stop_flag, fix_all_stop_key,
)


def _run(coro):
    return asyncio.run(coro)


class TestStopFlag(unittest.TestCase):
    def test_unset_by_default(self):
        flag = InMemoryStopFlag()
        self.assertFalse(_run(flag.is_set("k1")))

    def test_request_then_check(self):
        flag = InMemoryStopFlag()
        ok = _run(flag.request_stop("k1"))
        self.assertTrue(ok)
        self.assertTrue(_run(flag.is_set("k1")))

    def test_clear_removes_flag(self):
        flag = InMemoryStopFlag()
        _run(flag.request_stop("k1"))
        _run(flag.clear("k1"))
        self.assertFalse(_run(flag.is_set("k1")))

    def test_request_stop_idempotent(self):
        flag = InMemoryStopFlag()
        _run(flag.request_stop("k1"))
        _run(flag.request_stop("k1"))
        self.assertTrue(_run(flag.is_set("k1")))

    def test_distinct_keys_independent(self):
        flag = InMemoryStopFlag()
        _run(flag.request_stop("k1"))
        self.assertTrue(_run(flag.is_set("k1")))
        self.assertFalse(_run(flag.is_set("k2")))

    def test_make_stop_flag_in_memory_fallback(self):
        f = make_stop_flag(redis_url="")
        self.assertIsInstance(f, InMemoryStopFlag)

    def test_fix_all_stop_key_shape(self):
        self.assertEqual(
            fix_all_stop_key("p", 5), "fix_all:p:5",
        )


class TestProjectDeletion(unittest.TestCase):
    def test_delete_missing_returns_false(self):
        store = InMemoryLedgerStore()
        self.assertFalse(store.delete_project("does-not-exist"))

    def test_delete_existing_returns_true(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("p", "p")
        self.assertTrue(store.delete_project(pid))
        # Second call returns False — already gone.
        self.assertFalse(store.delete_project(pid))

    def test_delete_removes_ledger_entries(self):
        """After deletion the project's ledger entries should be gone."""
        store = InMemoryLedgerStore()
        pid = store.create_project("p", "p")
        store.write_entry(
            project_id=pid, tier=Tier.SPEC,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key="test:1",
            body={"x": "y"}, rationale="t", author="t",
        )
        before = len(store.all_current(pid, ArtifactKind.DECISION_RECORD))
        self.assertEqual(before, 1)

        store.delete_project(pid)
        after = len(store.all_current(pid, ArtifactKind.DECISION_RECORD))
        self.assertEqual(after, 0)

    def test_delete_isolated_to_target_project(self):
        """Deleting project A must not touch project B's data."""
        store = InMemoryLedgerStore()
        pid_a = store.create_project("a", "a")
        pid_b = store.create_project("b", "b")
        store.write_entry(
            project_id=pid_a, tier=Tier.SPEC,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key="test:1",
            body={"x": "a"}, rationale="t", author="t",
        )
        store.write_entry(
            project_id=pid_b, tier=Tier.SPEC,
            artifact_kind=ArtifactKind.DECISION_RECORD,
            artifact_key="test:1",
            body={"x": "b"}, rationale="t", author="t",
        )
        store.delete_project(pid_a)
        # B still has its record.
        b_entries = store.all_current(pid_b, ArtifactKind.DECISION_RECORD)
        self.assertEqual(len(b_entries), 1)


if __name__ == "__main__":
    unittest.main()
