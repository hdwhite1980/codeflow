"""Tests for the continuous indexer daemon (Turn C).

We don't drive a real Ollama through these tests. We exercise:
  - find_stale_paths: dedup logic for missing/changed/unchanged summaries
  - _list_projects: project enumeration
  - ContinuousIndexer.run: tick mechanics, inflight tracking, queue calls

Queue is a stub that records `enqueue` calls.
"""

from __future__ import annotations

import asyncio
import time
import unittest
from dataclasses import dataclass
from typing import Any

from continuous_indexer import (
    ContinuousIndexer, find_stale_paths, _interval_from_env,
)
from guardian_pipeline import FileSummary, write_file_summary
from ledger import ArtifactKind, Tier
from ledger_memory import InMemoryLedgerStore


def _run(coro):
    return asyncio.run(coro)


class StubQueue:
    """Records enqueue calls without actually running anything."""

    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue(self, job: dict[str, Any]) -> None:
        self.enqueued.append(job)

    async def close(self) -> None:
        pass


def _file_summary(path: str, content_hash: str = "") -> FileSummary:
    return FileSummary(
        file_path=path,
        plain_english="x", technical="y", purpose="z",
        touches=[], assumes=[], failure_modes=[], risk_notes=[],
        indexed_at=time.time(),
        indexer_model="qwen2.5-coder:7b",
        input_tokens=0, output_tokens=0,
        source_blob_sha256=content_hash,
    )


def _write_file(store, project_id: str, path: str, content: bytes) -> None:
    """Helper: write a FILE entry. The store hashes the blob internally
    via write_entry; we don't need to manage it separately."""
    store.write_entry(
        project_id=project_id, tier=Tier.GENERATION,
        artifact_kind=ArtifactKind.FILE,
        artifact_key=f"file:{project_id}:{path}",
        body=content.decode("utf-8"),
        rationale="test",
        author="test",
    )


def _file_blob_sha(store, project_id: str, path: str) -> str:
    """Look up the current FILE entry's blob_sha256 for a path."""
    for fe in store.all_current(project_id, ArtifactKind.FILE):
        parts = fe.artifact_key.split(":", 2)
        if len(parts) == 3 and parts[2] == path:
            return fe.blob_sha256
    return ""


class TestFindStalePaths(unittest.TestCase):
    def test_no_files_returns_empty(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        self.assertEqual(_run(find_stale_paths(store, pid)), [])

    def test_file_without_summary_is_stale(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _write_file(store, pid, "app/main.py", b"print('hi')")
        stale = _run(find_stale_paths(store, pid))
        self.assertEqual(stale, ["app/main.py"])

    def test_file_with_matching_summary_is_fresh(self):
        """File summary's source_blob_sha256 matches current file →
        not stale."""
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _write_file(store, pid, "a.py", b"x = 1")
        # Look up the actual blob sha that was assigned.
        file_sha = _file_blob_sha(store, pid, "a.py")
        write_file_summary(store, pid, _file_summary("a.py", content_hash=file_sha))
        stale = _run(find_stale_paths(store, pid))
        self.assertEqual(stale, [])

    def test_file_with_mismatched_summary_is_stale(self):
        """File summary points to old hash; current file has a new one.
        Should be flagged stale."""
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _write_file(store, pid, "a.py", b"x = 1")
        # Summary was indexed when content was different — use a fake
        # old hash that won't match the current file.
        write_file_summary(store, pid, _file_summary("a.py", content_hash="old_hash"))
        stale = _run(find_stale_paths(store, pid))
        self.assertEqual(stale, ["a.py"])

    def test_pre_turn_c_summary_without_hash_is_stale(self):
        """Legacy summary with empty source_blob_sha256: re-index once
        so future ticks have a hash to compare against."""
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        _write_file(store, pid, "a.py", b"x = 1")
        write_file_summary(store, pid, _file_summary("a.py", content_hash=""))
        stale = _run(find_stale_paths(store, pid))
        self.assertEqual(stale, ["a.py"])

    def test_deleted_file_not_flagged(self):
        """Files marked DELETED in iteration shouldn't be re-indexed."""
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        store.write_entry(
            project_id=pid, tier=Tier.GENERATION,
            artifact_kind=ArtifactKind.FILE,
            artifact_key=f"file:{pid}:gone.py",
            body="tombstone",
            rationale="DELETED in iteration 5",
            author="test",
        )
        stale = _run(find_stale_paths(store, pid))
        self.assertEqual(stale, [])


class TestContinuousIndexerTick(unittest.TestCase):
    def test_idle_when_no_projects(self):
        store = InMemoryLedgerStore()
        queue = StubQueue()
        indexer = ContinuousIndexer(store, queue, interval_seconds=0.01)
        _run(indexer._tick())
        self.assertEqual(queue.enqueued, [])

    def test_tick_queues_when_stale(self):
        store = InMemoryLedgerStore()
        queue = StubQueue()
        pid = store.create_project("test", "test")
        _write_file(store, pid, "a.py", b"x = 1")
        indexer = ContinuousIndexer(store, queue, interval_seconds=0.01)
        _run(indexer._tick())
        # Exactly one guardian_index job queued for the stale project.
        self.assertEqual(len(queue.enqueued), 1)
        self.assertEqual(queue.enqueued[0]["kind"], "guardian_index")
        self.assertEqual(queue.enqueued[0]["project_id"], pid)

    def test_tick_skips_inflight_projects(self):
        """Second tick without mark_project_done should NOT re-queue."""
        store = InMemoryLedgerStore()
        queue = StubQueue()
        pid = store.create_project("test", "test")
        _write_file(store, pid, "a.py", b"x = 1")
        indexer = ContinuousIndexer(store, queue, interval_seconds=0.01)
        _run(indexer._tick())
        _run(indexer._tick())
        # Still just one — second tick saw the project as inflight.
        self.assertEqual(len(queue.enqueued), 1)

    def test_tick_resumes_after_mark_done(self):
        store = InMemoryLedgerStore()
        queue = StubQueue()
        pid = store.create_project("test", "test")
        _write_file(store, pid, "a.py", b"x = 1")
        indexer = ContinuousIndexer(store, queue, interval_seconds=0.01)
        _run(indexer._tick())
        # Worker handler simulates completion.
        indexer.mark_project_done(pid)
        # But file is still stale (no summary written). Next tick should
        # re-queue.
        # Per-path rate limit is 60s though; advance the last_queued so
        # we don't hit it.
        indexer._last_queued.clear()
        _run(indexer._tick())
        self.assertEqual(len(queue.enqueued), 2)

    def test_per_path_rate_limit(self):
        """Even with mark_done, the same path shouldn't be re-queued
        within 60s."""
        store = InMemoryLedgerStore()
        queue = StubQueue()
        pid = store.create_project("test", "test")
        _write_file(store, pid, "a.py", b"x = 1")
        indexer = ContinuousIndexer(store, queue, interval_seconds=0.01)
        _run(indexer._tick())
        indexer.mark_project_done(pid)
        # Don't clear _last_queued — exercise the rate limit.
        _run(indexer._tick())
        self.assertEqual(len(queue.enqueued), 1)

    def test_fresh_project_does_not_queue(self):
        """Files exist and summaries match — daemon should idle."""
        store = InMemoryLedgerStore()
        queue = StubQueue()
        pid = store.create_project("test", "test")
        _write_file(store, pid, "a.py", b"x = 1")
        file_sha = _file_blob_sha(store, pid, "a.py")
        write_file_summary(store, pid, _file_summary("a.py", content_hash=file_sha))
        indexer = ContinuousIndexer(store, queue, interval_seconds=0.01)
        _run(indexer._tick())
        self.assertEqual(queue.enqueued, [])


class TestRunLoopStops(unittest.TestCase):
    def test_stop_event_exits_run(self):
        store = InMemoryLedgerStore()
        queue = StubQueue()
        indexer = ContinuousIndexer(store, queue, interval_seconds=0.01)
        # Set stop before run starts; loop should exit immediately
        # after the initial tick.
        indexer._stop_event.set()
        _run(indexer.run())
        # If we got here without hanging, the loop exited correctly.


class TestIntervalFromEnv(unittest.TestCase):
    def test_default_when_unset(self):
        import os
        os.environ.pop("GUARDIAN_CONTINUOUS_INTERVAL", None)
        self.assertEqual(_interval_from_env(default=42.0), 42.0)

    def test_uses_env_when_set(self):
        import os
        os.environ["GUARDIAN_CONTINUOUS_INTERVAL"] = "15"
        try:
            self.assertEqual(_interval_from_env(), 15.0)
        finally:
            os.environ.pop("GUARDIAN_CONTINUOUS_INTERVAL", None)

    def test_falls_back_on_bad_value(self):
        import os
        os.environ["GUARDIAN_CONTINUOUS_INTERVAL"] = "not-a-number"
        try:
            self.assertEqual(_interval_from_env(default=30.0), 30.0)
        finally:
            os.environ.pop("GUARDIAN_CONTINUOUS_INTERVAL", None)

    def test_sanity_floor_at_one(self):
        """Don't poll faster than once a second even if user asks."""
        import os
        os.environ["GUARDIAN_CONTINUOUS_INTERVAL"] = "0.001"
        try:
            self.assertEqual(_interval_from_env(), 1.0)
        finally:
            os.environ.pop("GUARDIAN_CONTINUOUS_INTERVAL", None)


if __name__ == "__main__":
    unittest.main()
