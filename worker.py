"""
codeflow.worker
===============

Background worker. Runs as a separate Railway service from the web API
(same image, CODEFLOW_ROLE=worker). Consumes build jobs from a Redis list,
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
"""

from __future__ import annotations

import asyncio
import os
import signal
import time
from typing import Any

from ledger import LedgerStore


# How often each reconciliation type runs. These intervals trade fresh state
# against API rate limits — Supabase has generous quotas, GitHub does not.
RECONCILE_INTERVALS_SECONDS = {
    "supabase": 5 * 60,        # every 5 min
    "github":   15 * 60,       # every 15 min (GitHub rate-limits aggressively)
    "railway":  10 * 60,       # every 10 min
}


class Worker:
    def __init__(self) -> None:
        self.database_url = os.environ["DATABASE_URL"]
        self.redis_url = os.environ.get("REDIS_URL", "")
        self.store = LedgerStore(database_url=self.database_url)
        self.shutdown = asyncio.Event()
        self._last_reconcile_at: dict[str, float] = {}

    async def run_forever(self) -> None:
        """Main loop. Alternates between build jobs and reconciliation ticks.

        We keep the loop sequential rather than running build jobs and
        reconciliation concurrently — a misbehaving build shouldn't be able
        to starve reconciliation, and vice versa. If throughput becomes an
        issue, split into two worker services with different roles."""
        print("[worker] started")
        while not self.shutdown.is_set():
            had_work = await self._maybe_process_build_job()
            await self._maybe_tick_reconciliations()
            if not had_work:
                # Nothing in the queue. Sleep briefly so we don't spin.
                try:
                    await asyncio.wait_for(self.shutdown.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass
        print("[worker] shutting down cleanly")

    async def _maybe_process_build_job(self) -> bool:
        """Pop one build job from the queue and process it.

        Stub for now — returns False so the loop falls through to
        reconciliation. The real implementation pops from a Redis list,
        deserializes the job, runs it through the Tier 1-4 pipeline, and
        writes outcomes to the ledger. We'll wire this up once the queue
        producer in app.py is implemented (the github_webhook handler will
        be the first producer)."""
        return False

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
        self.shutdown.set()


def main() -> None:
    worker = Worker()

    # Railway sends SIGTERM on redeploy. We have 30s to shut down cleanly.
    # The event-based shutdown lets the main loop exit at its next sleep.
    loop = asyncio.new_event_loop()
    loop.add_signal_handler(signal.SIGTERM, worker.request_shutdown)
    loop.add_signal_handler(signal.SIGINT, worker.request_shutdown)
    try:
        loop.run_until_complete(worker.run_forever())
    finally:
        loop.close()


if __name__ == "__main__":
    main()
