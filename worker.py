"""
codeflow.worker
===============

Background worker. Runs as a separate Railway service from the web API
(same image, CODEFLOW_ROLE=worker). Consumes build jobs from the queue,
runs them through the multi-AI pipeline, and writes results back through
the LedgerStore.

Why a separate process and not just BackgroundTasks
---------------------------------------------------
FastAPI's BackgroundTasks run inside the web request lifecycle. They share
the web process's memory and CPU, which is fine for sending an email but
fatal for a 60-second build that calls three frontier APIs in parallel. A
single slow build would head-of-line every other API request to that
instance.

Workers also let us scale the build path independently of the request path.
Webhook bursts hit the web service; long builds happen on worker replicas.
Railway makes this trivial — just bump the worker replica count.

Reconciliation schedule
-----------------------
The same worker process runs the periodic reconciliation jobs (Supabase
schema drift, Railway deploy state, GitHub poll-as-safety-net). We use a
simple "tick" loop rather than pulling in celery-beat or APScheduler — at
this scale a 10-line loop is more reliable than a scheduler library and
much easier to debug.

Loop fairness
-------------
Build jobs and reconciliation share one event loop. We process at most one
build job per tick before checking reconciliation timers, so a flood of
build jobs cannot starve the periodic ticks. Conversely, if a reconciliation
runs long, build jobs simply queue up in Redis and are served on the next
iteration — Redis is the buffer.
"""

from __future__ import annotations

import asyncio
import os
import signal
import time

from anthropic_client import AnthropicClient
from jobqueue import JobQueue, make_queue
from job_handlers import HandlerContext, dispatch
from ledger import LedgerStore
from openai_client import OpenAIClient
from usage_recorder import PostgresUsageRecorder


# How often each reconciliation type runs. These intervals trade fresh state
# against API rate limits — Supabase has generous quotas, GitHub does not.
RECONCILE_INTERVALS_SECONDS = {
    "supabase": 5 * 60,        # every 5 min
    "github":   15 * 60,       # every 15 min (GitHub rate-limits aggressively)
    "railway":  10 * 60,       # every 10 min
}

# How long a dequeue blocks before checking the shutdown flag and looping.
# Short enough that SIGTERM gets a fast response; long enough that we don't
# spin against Redis when idle.
DEQUEUE_TIMEOUT_SECONDS = 2.0


class Worker:
    def __init__(self) -> None:
        self.database_url = os.environ["DATABASE_URL"]
        self.redis_url = os.environ.get("REDIS_URL", "")
        self.anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
        self.openai_key = os.environ.get("OPENAI_API_KEY", "")
        self.store = LedgerStore(database_url=self.database_url)
        self.queue: JobQueue = make_queue(self.redis_url)
        # Each AI client is optional; the handlers refuse cleanly when their
        # required client is missing and write a diagnostic ledger entry.
        self.anthropic: AnthropicClient | None = (
            AnthropicClient(self.anthropic_key) if self.anthropic_key else None
        )
        self.openai: OpenAIClient | None = (
            OpenAIClient(self.openai_key) if self.openai_key else None
        )
        if self.anthropic is None:
            print("[worker] ANTHROPIC_API_KEY not set; build_project jobs "
                  "will write skip-records instead of running the pipeline.")
        if self.openai is None:
            print("[worker] OPENAI_API_KEY not set; audit_project jobs "
                  "will write skip-records instead of auditing.")
        self.ctx = HandlerContext(
            store=self.store,
            anthropic=self.anthropic,
            openai=self.openai,
            queue=self.queue,
            recorder=PostgresUsageRecorder(self.database_url),
        )
        self.shutdown = asyncio.Event()
        self._last_reconcile_at: dict[str, float] = {}

    async def run_forever(self) -> None:
        """Main loop. Alternates between build jobs and reconciliation ticks.

        We keep the loop sequential rather than running build jobs and
        reconciliation concurrently — a misbehaving build shouldn't be able
        to starve reconciliation, and vice versa. If throughput becomes an
        issue, split into two worker services with different roles."""
        print(f"[worker] started; queue backend = {type(self.queue).__name__}")
        try:
            while not self.shutdown.is_set():
                await self._maybe_process_build_job()
                await self._maybe_tick_reconciliations()
        finally:
            await self.queue.close()
            if self.anthropic is not None:
                await self.anthropic.aclose()
            if self.openai is not None:
                await self.openai.aclose()
            print("[worker] shut down cleanly")

    async def _maybe_process_build_job(self) -> None:
        """Pop one build job from the queue and dispatch it.

        Returns when either a job was processed or the dequeue timed out.
        Handler exceptions are caught inside the dispatcher; nothing here
        should ever raise back to the loop."""
        job = await self.queue.dequeue(timeout_seconds=DEQUEUE_TIMEOUT_SECONDS)
        if job is None:
            return
        print(f"[worker] received job kind={job.get('kind')!r}")
        await dispatch(job, self.ctx)

    async def _maybe_tick_reconciliations(self) -> None:
        """Run reconciliation jobs whose interval has elapsed."""
        now = time.monotonic()
        for kind, interval in RECONCILE_INTERVALS_SECONDS.items():
            last = self._last_reconcile_at.get(kind, 0.0)
            if now - last >= interval:
                await self._reconcile(kind)
                self._last_reconcile_at[kind] = now

    async def _reconcile(self, kind: str) -> None:
        """Per-kind reconciliation. Stubbed out — each one will use the
        connectors from runtime_sync.py once project metadata is wired up
        (we need a 'list of projects + their connector configs' table in
        Supabase first)."""
        print(f"[worker] reconcile tick: {kind}")
        # TODO:
        #  - load all active projects from Supabase
        #  - for each, build the right connector (DatabaseConnector,
        #    GitConnector, RailwayConnector) from stored config rows
        #  - call .reconcile() and write the summary to the ledger as a
        #    decision_record

    def request_shutdown(self) -> None:
        # Called from the signal handler. Setting the event lets the next
        # iteration of run_forever see it and exit cleanly.
        self.shutdown.set()


def main() -> None:
    worker = Worker()

    # Railway sends SIGTERM on redeploy. We have 30s to shut down cleanly.
    # The event-based shutdown lets the main loop exit at its next dequeue.
    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGTERM, worker.request_shutdown)
    loop.add_signal_handler(signal.SIGINT, worker.request_shutdown)
    try:
        loop.run_until_complete(worker.run_forever())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
