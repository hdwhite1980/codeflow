"""
codeflow.queue
==============

The job queue between the web service (producer) and the worker service
(consumer). Tiny abstraction over Redis lists with an in-memory implementation
for tests.

Design choices
--------------
We deliberately keep this stupid. Redis lists with LPUSH/BRPOP cover what we
need today (FIFO ordering, blocking pop, free with Railway's Redis plugin).
We don't reach for Celery, RQ, Dramatiq, or Sidekiq because:

  * They want their own conventions for serialization, decorators, and
    process management. We already have a worker.py that knows how to manage
    its own lifecycle and signal handling. Adopting a framework means
    learning its lifecycle on top of ours.
  * The expensive thing in a Code Flow build isn't the queue plumbing,
    it's the AI API calls. Whatever queue we pick will look the same
    sitting in front of those calls.
  * Migrating later is a one-file change. Migrating away from a framework
    that owns your worker process is a multi-day rewrite.

Job envelope
------------
Every job is a JSON object with at minimum:

    {
      "kind": "build_project" | "regenerate_artifact" | ...,
      "project_id": "<uuid>",
      "enqueued_at": "<iso8601>",
      ... handler-specific fields ...
    }

The `kind` field tells the worker which handler to dispatch to. Unknown
kinds get logged and dropped — we never crash the worker on an unrecognized
job because that would block the entire queue.

Visibility
----------
The web service enqueues; the worker dequeues. We could write each enqueue
to the ledger too (for full audit trail) but that doubles the write
amplification on a high-frequency path. Instead, the worker writes a single
"job_received" ledger entry at the start of processing — same auditability,
half the writes.

Failure semantics
-----------------
BRPOP removes the job atomically. If the worker crashes mid-job, the job is
lost. That's the tradeoff for not running a full job framework. For the
build pipeline this is fine: a failed build just gets re-triggered by the
user (or by the next webhook). If we add jobs where "exactly once" matters
(billing, etc.), we'll switch to BRPOPLPUSH with a reliable queue pattern.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import deque
from datetime import datetime, timezone
from typing import Any, Optional, Protocol


# Single shared queue name. We keep one queue for now — different job kinds
# share it. If certain kinds need different priorities later, split them out
# into named queues; the abstraction supports that with a `queue` argument.
DEFAULT_QUEUE = "codeflow:jobs"


def make_job(kind: str, **fields: Any) -> dict[str, Any]:
    """Build a job envelope with a consistent shape.

    Worker handlers should look up fields by name from the dict. We
    deliberately do not use Pydantic here — keeping the queue payload as a
    plain dict means we can add new kinds without coordinating model
    changes across web and worker deploys."""
    envelope: dict[str, Any] = {
        "kind": kind,
        "enqueued_at": datetime.now(timezone.utc).isoformat(),
    }
    envelope.update(fields)
    return envelope


# ---------------------------------------------------------------------------
# Protocol: the contract every backend must satisfy.
# ---------------------------------------------------------------------------

class JobQueue(Protocol):
    """Backend-agnostic interface. Both Redis and memory back-ends satisfy
    this; app.py and worker.py code against this protocol."""

    async def enqueue(self, job: dict[str, Any], queue: str = DEFAULT_QUEUE) -> None:
        ...

    async def dequeue(
        self, queue: str = DEFAULT_QUEUE, timeout_seconds: float = 2.0,
    ) -> Optional[dict[str, Any]]:
        """Block up to `timeout_seconds`, return one job or None on timeout.

        We use a short timeout instead of an indefinite block so the worker
        can interleave reconciliation work and respond promptly to SIGTERM
        on Railway redeploys."""
        ...

    async def queue_length(self, queue: str = DEFAULT_QUEUE) -> int:
        """How many jobs are waiting. Useful for /health and dashboards."""
        ...

    async def close(self) -> None:
        ...


# ---------------------------------------------------------------------------
# Redis backend — what runs in production.
# ---------------------------------------------------------------------------

class RedisJobQueue:
    """Redis-backed JobQueue. Uses LPUSH for enqueue and BRPOP for dequeue,
    making the queue strict FIFO (head pushed to left, tail popped from right).

    Connection management
    ---------------------
    redis-py's asyncio client is connection-pooled internally. We hold the
    client on the instance and let the pool deal with reconnects. The
    `connection_pool` defaults are fine for our throughput; no need to tune."""

    def __init__(self, redis_url: str) -> None:
        # Lazy import so that tests using MemoryJobQueue don't have to
        # install redis. Also avoids any import-time side effects from the
        # redis client touching event loops.
        import redis.asyncio as redis_async  # type: ignore[import-untyped]
        self._redis = redis_async.from_url(
            redis_url,
            decode_responses=True,        # we serialize JSON ourselves
            health_check_interval=30,     # ping idle conns so we notice drops
        )

    async def enqueue(self, job: dict[str, Any], queue: str = DEFAULT_QUEUE) -> None:
        payload = json.dumps(job, separators=(",", ":"))
        await self._redis.lpush(queue, payload)

    async def dequeue(
        self, queue: str = DEFAULT_QUEUE, timeout_seconds: float = 2.0,
    ) -> Optional[dict[str, Any]]:
        # BRPOP returns (queue_name, payload) or None on timeout. Redis
        # expects the timeout as an integer in older protocol versions; we
        # round up to be safe.
        result = await self._redis.brpop([queue], timeout=max(1, int(timeout_seconds)))
        if result is None:
            return None
        _name, payload = result
        try:
            return json.loads(payload)
        except json.JSONDecodeError:
            # Corrupt payload — we'd rather log and drop than crash the
            # consumer loop. A poisoned message would otherwise block forever.
            print(f"[queue] dropped malformed job payload: {payload!r}")
            return None

    async def queue_length(self, queue: str = DEFAULT_QUEUE) -> int:
        return int(await self._redis.llen(queue))

    async def close(self) -> None:
        await self._redis.aclose()


# ---------------------------------------------------------------------------
# In-memory backend — tests and local dev without Redis running.
# ---------------------------------------------------------------------------

class MemoryJobQueue:
    """asyncio-friendly in-process queue. Same semantics as the Redis backend
    for the fields we care about (FIFO, blocking dequeue with timeout).

    Not suitable for production: state is lost on process restart and is not
    shared across processes. Producers and consumers must be in the same
    Python process."""

    def __init__(self) -> None:
        self._queues: dict[str, deque[dict[str, Any]]] = {}
        self._cond = asyncio.Condition()

    async def enqueue(self, job: dict[str, Any], queue: str = DEFAULT_QUEUE) -> None:
        async with self._cond:
            self._queues.setdefault(queue, deque()).append(job)
            self._cond.notify_all()

    async def dequeue(
        self, queue: str = DEFAULT_QUEUE, timeout_seconds: float = 2.0,
    ) -> Optional[dict[str, Any]]:
        async with self._cond:
            try:
                # Wait until either a job arrives or we time out.
                await asyncio.wait_for(
                    self._cond.wait_for(
                        lambda: bool(self._queues.get(queue))
                    ),
                    timeout=timeout_seconds,
                )
            except asyncio.TimeoutError:
                return None
            return self._queues[queue].popleft()

    async def queue_length(self, queue: str = DEFAULT_QUEUE) -> int:
        return len(self._queues.get(queue, ()))

    async def close(self) -> None:
        # Nothing to close — drop the references so GC can reclaim.
        self._queues.clear()


# ---------------------------------------------------------------------------
# Factory.
# ---------------------------------------------------------------------------

def make_queue(redis_url: Optional[str] = None) -> JobQueue:
    """Construct a queue from configuration.

    If `redis_url` is non-empty we return a RedisJobQueue. If it's missing
    (e.g. local dev without Redis, or unit tests), we return an in-memory
    queue. This keeps `app.py` and `worker.py` working in CI without any
    test-specific branching at the call site — but for production, you
    really want Redis."""
    if redis_url is None:
        redis_url = os.environ.get("REDIS_URL", "")
    if redis_url:
        return RedisJobQueue(redis_url)
    print("[queue] REDIS_URL not set; using in-memory queue (NOT FOR PRODUCTION)")
    return MemoryJobQueue()
