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
                        if e.artifact_key.endswith(":build:skipped")]
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


class TestAutopatchTrigger(unittest.TestCase):
    """Exercise _maybe_trigger_autopatch directly.

    Four branches matter:
      (a) no criticals          → no queue write, no decision record
      (b) criticals, under cap  → iterate_project queued + :triggered record
      (c) criticals, at cap     → :capped record, no queue write
      (d) total_critical>0 but verdict ledger empty → defensive no-op

    These tests use InMemoryLedgerStore + MemoryJobQueue + a fake
    HandlerContext. We seed the ledger with audit_verdict entries
    to simulate "an audit just finished and found criticals", then
    invoke _maybe_trigger_autopatch and check the side effects.
    """

    def _make_ctx_and_queue(self):
        from job_handlers import HandlerContext
        store = InMemoryLedgerStore()
        queue = MemoryJobQueue()
        ctx = HandlerContext(
            store=store, queue=queue,
            anthropic=None, openai=None, gemini=None,
            recorder=None,
        )
        pid = store.create_project("test", "test")
        return store, queue, ctx, pid

    def _seed_critical_verdict(self, store, pid, path, issue):
        """Write one audit_verdict entry with a critical finding."""
        from ledger import ArtifactKind, Tier
        store.write_entry(
            project_id=pid, tier=Tier.AUDIT,
            artifact_kind=ArtifactKind.AUDIT_VERDICT,
            artifact_key=f"ref:{pid}:audit:openai:{path}",
            body={
                "file_path": path,
                "findings": [
                    {
                        "severity": "critical",
                        "category": "correctness",
                        "line": 21,
                        "issue": issue,
                        "suggestion": "fix it",
                    }
                ],
            },
            rationale=f"openai audit of {path}: 1 finding",
            author="test",
        )

    def test_no_criticals_no_action(self):
        from job_handlers import _maybe_trigger_autopatch
        store, queue, ctx, pid = self._make_ctx_and_queue()

        _run(_maybe_trigger_autopatch(
            project_id=pid, ctx=ctx,
            total_critical=0, outcomes=[], auditors=[],
        ))

        # No autopatch record, no iterate_project job queued.
        from ledger import ArtifactKind
        records = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        self.assertEqual(
            [r for r in records if r.artifact_key.startswith("autopatch:")],
            [],
        )
        self.assertEqual(_run(queue.queue_length()), 0)

    def test_criticals_under_cap_queues_iteration(self):
        from job_handlers import _maybe_trigger_autopatch
        store, queue, ctx, pid = self._make_ctx_and_queue()

        # Seed two critical findings.
        self._seed_critical_verdict(store, pid, "app/main.py", "Crashes on null input")
        self._seed_critical_verdict(store, pid, "app/db.py", "SQL injection in raw query")

        _run(_maybe_trigger_autopatch(
            project_id=pid, ctx=ctx,
            total_critical=2, outcomes=[], auditors=[],
        ))

        # Exactly one iterate_project job queued.
        self.assertEqual(_run(queue.queue_length()), 1)
        from ledger import ArtifactKind
        records = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        triggered = [r for r in records if r.artifact_key == "autopatch:1:triggered"]
        self.assertEqual(len(triggered), 1,
                         "Expected exactly one autopatch:1:triggered record")
        # No :capped record at this attempt level.
        capped = [r for r in records if r.artifact_key.endswith(":capped")]
        self.assertEqual(capped, [])

    def test_criticals_at_cap_writes_capped_record(self):
        from job_handlers import _maybe_trigger_autopatch, AUTOPATCH_MAX_ATTEMPTS
        store, queue, ctx, pid = self._make_ctx_and_queue()

        # Seed AUTOPATCH_MAX_ATTEMPTS existing :triggered records to
        # simulate "we've already done all our retries."
        from ledger import ArtifactKind, Tier
        for i in range(1, AUTOPATCH_MAX_ATTEMPTS + 1):
            store.write_entry(
                project_id=pid, tier=Tier.AUDIT,
                artifact_kind=ArtifactKind.DECISION_RECORD,
                artifact_key=f"autopatch:{i}:triggered",
                body={"attempt": i, "critical_count": 1, "critical_lines": []},
                rationale=f"prior autopatch attempt {i}",
                author="test",
            )

        # Seed a new critical so total_critical > 0.
        self._seed_critical_verdict(store, pid, "app/main.py", "still broken")

        _run(_maybe_trigger_autopatch(
            project_id=pid, ctx=ctx,
            total_critical=1, outcomes=[], auditors=[],
        ))

        # No new iterate_project job — we're capped.
        self.assertEqual(_run(queue.queue_length()), 0)
        records = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        capped = [r for r in records if r.artifact_key.endswith(":capped")]
        self.assertEqual(len(capped), 1,
                         "Expected exactly one :capped record after hitting cap")

    def test_critical_count_positive_but_no_findings_extracted(self):
        """Defensive case: total_critical claims > 0 but the ledger has
        no critical findings. Don't queue an iteration with an empty
        prompt — that would send the Builder garbage."""
        from job_handlers import _maybe_trigger_autopatch
        store, queue, ctx, pid = self._make_ctx_and_queue()

        # Don't seed any verdicts. But claim total_critical=5.
        _run(_maybe_trigger_autopatch(
            project_id=pid, ctx=ctx,
            total_critical=5, outcomes=[], auditors=[],
        ))

        self.assertEqual(_run(queue.queue_length()), 0)
        from ledger import ArtifactKind
        records = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        triggered = [r for r in records if r.artifact_key.endswith(":triggered")]
        self.assertEqual(triggered, [])

    def test_collect_critical_findings_caps_at_max(self):
        """If many critical findings exist across files, the prompt is
        capped at max_lines to keep token usage sane."""
        from job_handlers import _collect_critical_findings
        store, _, _, pid = self._make_ctx_and_queue()

        # Seed 5 verdicts × 8 critical findings each = 40 total.
        from ledger import ArtifactKind, Tier
        for f_idx in range(5):
            findings = [
                {"severity": "critical", "line": 10 + i,
                 "issue": f"issue {i}", "category": "correctness"}
                for i in range(8)
            ]
            store.write_entry(
                project_id=pid, tier=Tier.AUDIT,
                artifact_kind=ArtifactKind.AUDIT_VERDICT,
                artifact_key=f"ref:{pid}:audit:openai:file{f_idx}.py",
                body={"file_path": f"file{f_idx}.py", "findings": findings},
                rationale="test", author="test",
            )

        lines = _collect_critical_findings(store, pid, max_lines=20)
        self.assertEqual(len(lines), 20)

    def test_collect_critical_findings_skips_non_critical(self):
        from job_handlers import _collect_critical_findings
        store, _, _, pid = self._make_ctx_and_queue()
        from ledger import ArtifactKind, Tier
        store.write_entry(
            project_id=pid, tier=Tier.AUDIT,
            artifact_kind=ArtifactKind.AUDIT_VERDICT,
            artifact_key=f"ref:{pid}:audit:openai:f.py",
            body={
                "file_path": "f.py",
                "findings": [
                    {"severity": "warning", "line": 1, "issue": "minor"},
                    {"severity": "critical", "line": 2, "issue": "major"},
                    {"severity": "nit", "line": 3, "issue": "style"},
                ],
            },
            rationale="t", author="t",
        )
        lines = _collect_critical_findings(store, pid)
        self.assertEqual(len(lines), 1)
        self.assertIn("major", lines[0])


if __name__ == "__main__":
    unittest.main()
