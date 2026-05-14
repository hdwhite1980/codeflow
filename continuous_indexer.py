"""
codeflow.continuous_indexer
===========================

Background daemon that keeps the guardian's memory in sync with the
ledger.

Why this exists
---------------
Indexing is normally triggered by events: build completes, iteration
completes, import completes, manual POST /guardian/index. The G-D fix
ensured every build and iterate auto-queues an index. But things still
fall through cracks:

  * Files written outside the normal handler flow (future webhook sync,
    one-off ledger writes)
  * Handler crashes that succeed in writing the file but fail before
    queuing the index
  * Cases where indexing was queued but a file's been re-written since
    (stale summary)

The daemon plugs these holes by polling. Every interval it asks each
project: "any FILE entries whose blob_sha256 differs from the most
recent summary's source_blob_sha256?" If yes, queue a per-file index.

Polling, not subscribing
------------------------
We poll Postgres rather than wire into Supabase realtime. Reasons:
  * Simpler to reason about and test deterministically
  * Already paying for the connection; one query/tick is negligible
  * Realtime adds another failure surface (channel disconnect, missed
    events on reconnect)

We can switch to subscribe-driven later if polling load becomes real.
For now, 30-second tick × handful of projects = trivial DB cost.

Dedup
-----
Each FileSummary now carries source_blob_sha256 — the hash of the
content that produced the summary. The daemon compares each file's
current blob_sha256 to the matching summary's hash. Match = no work.
Mismatch (or no summary at all) = queue an index for that path.

Coalescing
----------
If a project already has pending guardian_index jobs in the queue,
skip that project for this tick. Avoids stacking N redundant jobs
when files are changing fast.

State
-----
In-memory per-process. On daemon restart, the first tick re-checks
every project — the dedup-by-hash logic means stale work isn't queued,
just real diffs. Stateless restart is the right model: no DB schema
changes, no risk of state drift after a long outage.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any, Optional

from jobqueue import JobQueue, make_job
from ledger import ArtifactKind


# How often to poll. Conservative default — most projects don't change
# this fast and indexing is expensive on CPU Ollama. The daemon does
# real work only when there's a diff, so the tick cost is just one
# query per project even when nothing's changed.
DEFAULT_POLL_INTERVAL_SECONDS = 30.0

# Optional override via env var so production can dial without a deploy.
_ENV_INTERVAL = "GUARDIAN_CONTINUOUS_INTERVAL"


def _interval_from_env(default: float = DEFAULT_POLL_INTERVAL_SECONDS) -> float:
    raw = os.environ.get(_ENV_INTERVAL, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
        if v < 1.0:  # sanity floor
            return 1.0
        return v
    except ValueError:
        return default


async def find_stale_paths(
    store: Any, project_id: str,
) -> list[str]:
    """Return file paths whose current blob_sha256 differs from the
    matching summary's source_blob_sha256.

    Cases that produce a "stale" classification (path returned):
      1. File has no summary at all yet → needs initial index
      2. File's blob_sha256 != summary.source_blob_sha256 → content changed
      3. Summary lacks a source_blob_sha256 (legacy / pre-Turn-C) → can't
         confirm freshness, treat as stale for safety. Daemon will re-
         index once; subsequent ticks see the new hash and skip.
    """
    from guardian_pipeline import load_file_summaries

    # Current files in the project. The all_current view already
    # filters to current versions per artifact_key, so we don't have
    # to deal with superseded entries.
    file_entries = store.all_current(project_id, ArtifactKind.FILE)
    if not file_entries:
        return []

    # Map summaries by file_path for fast lookup. We expect at most one
    # current summary per path.
    summaries = load_file_summaries(store, project_id)
    summary_hash_by_path: dict[str, str] = {}
    for s in summaries:
        path = s.get("file_path", "")
        if not path:
            continue
        summary_hash_by_path[path] = s.get("source_blob_sha256", "") or ""

    stale: list[str] = []
    for fe in file_entries:
        # artifact_key shape: file:<pid>:<path>
        parts = fe.artifact_key.split(":", 2)
        if len(parts) < 3:
            continue
        path = parts[2]

        # Files explicitly marked deleted by an iteration shouldn't be
        # re-indexed — they're tombstoned.
        if "DELETED in iteration" in (fe.rationale or ""):
            continue

        current_hash = fe.blob_sha256 or ""
        summary_hash = summary_hash_by_path.get(path)

        if summary_hash is None:
            # No summary yet for this path. Initial index needed.
            stale.append(path)
            continue

        if summary_hash == "":
            # Pre-Turn-C summary without a recorded source hash. Re-index
            # once so future ticks have a hash to compare against.
            stale.append(path)
            continue

        if current_hash and current_hash != summary_hash:
            stale.append(path)

    return stale


async def _list_projects(store: Any) -> list[str]:
    """Return the project IDs the daemon should poll.

    Both stores expose `list_projects(limit=...)` that returns
    list[dict[str, Any]] with at least an "id" key. Returns [] when the
    method isn't available — the daemon then idles harmlessly.
    """
    if hasattr(store, "list_projects"):
        rows = store.list_projects()
        out: list[str] = []
        for row in rows:
            pid = (
                row.get("id") if isinstance(row, dict)
                else getattr(row, "id", None)
            )
            if pid:
                out.append(str(pid))
        return out
    return []


class ContinuousIndexer:
    """Owns the polling loop. Constructed once per worker process.

    Usage:
        indexer = ContinuousIndexer(store, queue)
        task = asyncio.create_task(indexer.run())
        ...later...
        indexer.stop()
        await task
    """

    def __init__(
        self, store: Any, queue: JobQueue,
        *, interval_seconds: Optional[float] = None,
    ) -> None:
        self._store = store
        self._queue = queue
        self._interval = (
            interval_seconds
            if interval_seconds is not None
            else _interval_from_env()
        )
        self._stop_event = asyncio.Event()
        # Per-project pending-job tracker. If a project already has an
        # index queued from a prior tick (or from a normal G-D event),
        # we skip queuing more until the prior one completes. The job
        # handler clears the flag when it finishes.
        self._inflight: set[str] = set()
        # Per-path last-queued time, for client-side rate limiting in
        # case the queue's own dedup ever fails. 60s minimum between
        # consecutive queue calls for the same path.
        self._last_queued: dict[tuple[str, str], float] = {}
        self._tick_count = 0

    def stop(self) -> None:
        """Signal the loop to exit at the next tick boundary."""
        self._stop_event.set()

    async def run(self) -> None:
        """Polling loop. Returns when stop() is called or the task is
        cancelled."""
        print(f"[continuous_indexer] starting (interval={self._interval}s)",
              flush=True)
        while not self._stop_event.is_set():
            try:
                await self._tick()
            except Exception as exc:
                # Daemon stays alive even when one tick blows up; loud log
                # and try again next interval. Include a traceback so
                # production bugs are debuggable from the log line alone
                # without having to add temporary instrumentation.
                import traceback
                print(f"[continuous_indexer] tick failed: "
                      f"{type(exc).__name__}: {exc}\n"
                      f"{traceback.format_exc()}", flush=True)
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._interval,
                )
            except asyncio.TimeoutError:
                pass  # normal — proceed to next tick
        print("[continuous_indexer] stopped", flush=True)

    async def _tick(self) -> None:
        """One pass over all projects. Public-ish for tests."""
        self._tick_count += 1
        projects = await _list_projects(self._store)
        if not projects:
            return

        now = time.time()
        any_queued = False

        for project_id in projects:
            if project_id in self._inflight:
                # We queued an index for this project in a previous tick
                # and don't know it finished yet. Skip; the handler will
                # clear the flag.
                continue

            try:
                stale_paths = await find_stale_paths(
                    self._store, project_id,
                )
            except Exception as exc:
                print(f"[continuous_indexer] {project_id}: scan failed: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                continue

            if not stale_paths:
                continue

            # Per-path 60s rate limit. Without this, a flaky handler that
            # never marks completion could cause the daemon to queue the
            # same path every tick. The inflight set above is the main
            # guard; this is belt-and-suspenders.
            paths_to_queue = []
            for path in stale_paths:
                last = self._last_queued.get((project_id, path), 0.0)
                if now - last < 60.0:
                    continue
                paths_to_queue.append(path)
                self._last_queued[(project_id, path)] = now

            if not paths_to_queue:
                continue

            # Queue ONE job per project that covers all stale paths in
            # this tick. The job handler iterates target_path-free
            # (covers everything stale in the project). Simpler than N
            # individual jobs and the handler's per-file dedup logic
            # ensures it doesn't re-index files that were caught up
            # between scan and execute.
            await self._queue.enqueue(make_job(
                "guardian_index",
                project_id=project_id,
            ))
            self._inflight.add(project_id)
            any_queued = True
            print(f"[continuous_indexer] tick #{self._tick_count}: "
                  f"queued guardian_index for {project_id} "
                  f"({len(paths_to_queue)} stale paths)", flush=True)

        if not any_queued and self._tick_count % 20 == 0:
            # Quieter heartbeat every 20 idle ticks so production logs
            # confirm the daemon is alive without spamming.
            print(f"[continuous_indexer] tick #{self._tick_count}: idle",
                  flush=True)

    def mark_project_done(self, project_id: str) -> None:
        """Called by the job handler when guardian_index completes for
        a project. Clears the inflight flag so the next tick can re-
        evaluate. If never called the daemon will eventually retry
        after the inflight entry ages out via a daemon restart, but
        that's a fallback, not the happy path."""
        self._inflight.discard(project_id)
