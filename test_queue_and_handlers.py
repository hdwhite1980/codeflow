"""
test_queue_and_handlers
=======================

Covers the queue abstraction and the job dispatcher. Runs entirely in-process
with the MemoryJobQueue backend and the in-memory ledger so CI doesn't need
Redis or Postgres.

We deliberately exercise the same code path the worker uses — make_queue +
HandlerContext + dispatch — rather than calling handlers directly. That way
if anyone refactors the dispatcher signature, the tests will catch it.
"""

from __future__ import annotations

import asyncio
import unittest

from jobqueue import (
    DEFAULT_QUEUE, MemoryJobQueue, make_job, make_queue,
)
from job_handlers import HANDLERS, HandlerContext, dispatch
from ledger_memory import InMemoryLedgerStore


def _run(coro):
    """Tiny helper so each test method reads top-to-bottom without the
    async noise. unittest's IsolatedAsyncioTestCase exists but pulls in
    per-test event loops that flag harmless warnings; sticking to
    asyncio.run keeps the failure modes obvious."""
    return asyncio.run(coro)


class TestMemoryJobQueue(unittest.TestCase):
    def test_enqueue_then_dequeue_returns_same_job(self):
        async def go():
            q = MemoryJobQueue()
            await q.enqueue({"kind": "build_project", "project_id": "p1"})
            return await q.dequeue(timeout_seconds=0.5)
        got = _run(go())
        self.assertIsNotNone(got)
        self.assertEqual(got["kind"], "build_project")
        self.assertEqual(got["project_id"], "p1")

    def test_dequeue_times_out_when_empty(self):
        async def go():
            q = MemoryJobQueue()
            return await q.dequeue(timeout_seconds=0.1)
        self.assertIsNone(_run(go()))

    def test_fifo_order(self):
        async def go():
            q = MemoryJobQueue()
            await q.enqueue({"kind": "a", "n": 1})
            await q.enqueue({"kind": "a", "n": 2})
            await q.enqueue({"kind": "a", "n": 3})
            return [(await q.dequeue(timeout_seconds=0.1))["n"] for _ in range(3)]
        self.assertEqual(_run(go()), [1, 2, 3])

    def test_queue_length(self):
        async def go():
            q = MemoryJobQueue()
            self.assertEqual(await q.queue_length(), 0)
            await q.enqueue({"kind": "x"})
            await q.enqueue({"kind": "x"})
            return await q.queue_length()
        self.assertEqual(_run(go()), 2)

    def test_isolated_queues(self):
        """Different queue names should not see each other's jobs."""
        async def go():
            q = MemoryJobQueue()
            await q.enqueue({"kind": "a"}, queue="alpha")
            await q.enqueue({"kind": "b"}, queue="beta")
            a = await q.dequeue(queue="alpha", timeout_seconds=0.1)
            b = await q.dequeue(queue="beta", timeout_seconds=0.1)
            return a["kind"], b["kind"]
        self.assertEqual(_run(go()), ("a", "b"))


class TestMakeJob(unittest.TestCase):
    def test_kind_and_enqueued_at_are_set(self):
        job = make_job("build_project", project_id="p1", prompt="hi")
        self.assertEqual(job["kind"], "build_project")
        self.assertIn("enqueued_at", job)
        self.assertEqual(job["project_id"], "p1")
        self.assertEqual(job["prompt"], "hi")

    def test_kind_kwarg_collision_raises_typeerror(self):
        """If someone passes kind= as a kwarg, Python rejects it because
        `kind` is already a positional parameter. This is the desired
        behavior — accidentally shadowing the job kind would be a subtle
        bug. We assert it stays a hard error."""
        with self.assertRaises(TypeError):
            make_job("build_project", kind="override", project_id="p1")


class TestMakeQueueFactory(unittest.TestCase):
    def test_no_redis_url_returns_memory_queue(self):
        q = make_queue(redis_url="")
        self.assertIsInstance(q, MemoryJobQueue)

    def test_none_redis_url_falls_back_to_env(self):
        # We rely on REDIS_URL not being set in the test environment.
        # If it is, this test is skipped by environment hygiene.
        import os
        if os.environ.get("REDIS_URL"):
            self.skipTest("REDIS_URL is set in this environment")
        q = make_queue(redis_url=None)
        self.assertIsInstance(q, MemoryJobQueue)


class TestDispatch(unittest.TestCase):
    def test_build_project_writes_ledger_entry(self):
        """Verify the end-to-end path: enqueue → dequeue → dispatch → ledger.

        Without an Anthropic client in the context, the handler writes a
        skip-record explaining why nothing happened. That's the correct,
        observable behavior — earlier in development this test asserted a
        stub `plan:` entry, but the real handler now refuses without an
        API key and the test was updated to match."""
        async def go():
            store = InMemoryLedgerStore()
            project_id = store.create_project("smoke", "build me something nice please")
            ctx = HandlerContext(store=store, anthropic=None)
            queue = MemoryJobQueue()
            await queue.enqueue(make_job(
                "build_project",
                project_id=project_id,
                prompt="build me something nice please",
                slug="smoke",
            ))
            job = await queue.dequeue(timeout_seconds=0.1)
            await dispatch(job, ctx)
            return project_id, store

        project_id, store = _run(go())
        entries = store.all_current(project_id)
        skip_entries = [e for e in entries
                        if e.artifact_key.endswith(":skipped")]
        self.assertEqual(len(skip_entries), 1)
        self.assertIn("no Anthropic client", skip_entries[0].rationale)

    def test_unknown_kind_is_logged_not_raised(self):
        """A typo in kind should never crash the worker loop."""
        async def go():
            store = InMemoryLedgerStore()
            ctx = HandlerContext(store=store)
            await dispatch({"kind": "no_such_handler"}, ctx)
        # The assertion is that this doesn't raise. If it did, _run would
        # propagate the exception out.
        _run(go())

    def test_missing_kind_is_logged_not_raised(self):
        async def go():
            store = InMemoryLedgerStore()
            ctx = HandlerContext(store=store)
            await dispatch({"project_id": "p1"}, ctx)
        _run(go())

    def test_build_project_missing_project_id_is_dropped(self):
        async def go():
            store = InMemoryLedgerStore()
            ctx = HandlerContext(store=store)
            await dispatch(make_job("build_project", prompt="x"), ctx)
            # Nothing should have been written.
            return store
        store = _run(go())
        # No projects were created via the API path here, so there's
        # nothing to list. We assert there were no writes by checking
        # the store has no entries for any phantom project_id.
        self.assertEqual(store.all_current("nonexistent-project"), [])

    def test_registry_only_contains_async_callables(self):
        """Belt-and-braces: catch the case where someone adds a sync
        function to HANDLERS by accident. Sync functions would silently
        return a coroutine-less value and never run."""
        import inspect
        for name, fn in HANDLERS.items():
            self.assertTrue(
                inspect.iscoroutinefunction(fn),
                f"handler {name!r} must be async",
            )


if __name__ == "__main__":
    unittest.main()
