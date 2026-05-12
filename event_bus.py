"""
codeflow.event_bus
==================

Publish/subscribe abstraction for project-scoped events.

Two backends:
  * RedisEventBus — production. Uses redis pub/sub. The worker publishes;
    the web service subscribes per WebSocket connection.
  * InMemoryEventBus — tests. Records published events for inspection
    and supports synchronous subscribe.

Why pub/sub instead of just polling the ledger
------------------------------------------------
We could have the WebSocket endpoint poll Postgres on a timer, but:
  - Postgres polling is wasteful (every connected client = 1 query/sec).
  - Pub/sub is push-based; events land within ~10ms of being emitted.
  - Redis is already in our stack; no new dependency.
  - The worker already writes everything we want to publish — adding
    one PUBLISH per ledger write costs nothing.

Channel naming
--------------
We use one channel per project: `project:<project_id>`. The web service
subscribes only to the channel(s) it has active WebSocket clients for,
which keeps message routing simple — Redis only delivers to interested
subscribers.

Message shape
-------------
Every event is JSON with at least `kind` and `project_id`. Common kinds:
  - "ledger_entry"  — a new artifact landed
  - "usage_row"     — a new API call was recorded
  - "build_started" / "build_completed"
  - "audit_started" / "audit_completed"
The frontend dispatches on `kind` and updates its local state.

Failure semantics
-----------------
publish() is best-effort. If Redis is down or the channel is full, we
log and drop. We never block the worker on a UI delivery — the ledger
is the source of truth, and the frontend can always re-fetch via REST
to recover.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, AsyncIterator, Optional


class EventBus:
    """Abstract base. Two methods: publish (one-shot, fire-and-forget)
    and subscribe (async generator yielding events for a project)."""

    async def publish(self, project_id: str, event: dict[str, Any]) -> None:
        raise NotImplementedError

    async def subscribe(self, project_id: str) -> AsyncIterator[dict[str, Any]]:
        # Subclasses implement this as an async generator. Declaring it
        # async generator-style here is awkward in ABCs, so we leave the
        # type annotation and let subclasses do the real work.
        raise NotImplementedError
        yield  # pragma: no cover  — makes this a generator for type purposes


# ---------------------------------------------------------------------------
# Redis backend.
# ---------------------------------------------------------------------------

class RedisEventBus(EventBus):
    """Production backend. Pub/sub over the same Redis instance the job
    queue uses. Lazy-imports redis so non-production code paths (tests)
    don't pull it in.

    Each publish() acquires a connection, fires PUBLISH, releases. The
    asyncio redis client is connection-pooled internally, so this is
    cheap; we don't need to hold a persistent connection here.

    Each subscribe() holds one subscriber connection for the lifetime of
    the WebSocket. When the WS closes, the caller stops iterating and
    we unsubscribe cleanly."""

    def __init__(self, redis_url: str) -> None:
        if not redis_url:
            raise ValueError("RedisEventBus requires a non-empty REDIS_URL")
        # Lazy import so tests on machines without redis installed still work.
        import redis.asyncio as redis_async
        self._redis_async = redis_async
        self._url = redis_url
        # One client for publishes, reused. Connections are pooled internally.
        self._publisher = redis_async.from_url(redis_url, decode_responses=True)

    async def publish(self, project_id: str, event: dict[str, Any]) -> None:
        try:
            payload = json.dumps(event, default=str)
            await self._publisher.publish(f"project:{project_id}", payload)
        except Exception as exc:
            # Don't let pub/sub failures take down the worker. The ledger
            # is the source of truth; the UI can always re-fetch via REST.
            print(f"[event_bus] publish failed for project {project_id}: "
                  f"{type(exc).__name__}: {exc}", flush=True)

    async def subscribe(self, project_id: str) -> AsyncIterator[dict[str, Any]]:
        """Yield events for one project. The caller is responsible for
        stopping iteration (e.g. by closing the WebSocket); we'll
        unsubscribe and close the connection in finally."""
        client = self._redis_async.from_url(self._url, decode_responses=True)
        pubsub = client.pubsub()
        channel = f"project:{project_id}"
        try:
            await pubsub.subscribe(channel)
            async for msg in pubsub.listen():
                # listen() yields both subscribe confirmations and real
                # messages. We only care about the latter.
                if msg.get("type") != "message":
                    continue
                data = msg.get("data")
                if not data:
                    continue
                try:
                    event = json.loads(data)
                except Exception:
                    print(f"[event_bus] subscriber got unparseable message "
                          f"on {channel}: {data[:200]!r}", flush=True)
                    continue
                yield event
        finally:
            try:
                await pubsub.unsubscribe(channel)
                await pubsub.aclose()
            except Exception:
                pass
            try:
                await client.aclose()
            except Exception:
                pass

    async def aclose(self) -> None:
        try:
            await self._publisher.aclose()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# In-memory backend for tests.
# ---------------------------------------------------------------------------

class InMemoryEventBus(EventBus):
    """Test backend. Each project gets its own asyncio.Queue per active
    subscriber. publish() puts the event on every subscriber's queue.

    Not for production: no cross-process delivery, no persistence."""

    def __init__(self) -> None:
        # project_id -> list of subscriber queues
        self._subscribers: dict[str, list[asyncio.Queue]] = {}
        # All published events, in order, for test assertions.
        self.published: list[tuple[str, dict[str, Any]]] = []

    async def publish(self, project_id: str, event: dict[str, Any]) -> None:
        self.published.append((project_id, event))
        for q in self._subscribers.get(project_id, []):
            await q.put(event)

    async def subscribe(self, project_id: str) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.setdefault(project_id, []).append(queue)
        try:
            while True:
                event = await queue.get()
                yield event
        finally:
            self._subscribers[project_id].remove(queue)


# ---------------------------------------------------------------------------
# Factory.
# ---------------------------------------------------------------------------

def make_event_bus(redis_url: Optional[str] = None) -> EventBus:
    """Build a Redis bus when REDIS_URL is set, otherwise in-memory.

    In production the worker and web service both pass REDIS_URL and
    get a real distributed bus. In tests we get an in-memory bus that
    can be asserted against."""
    url = redis_url if redis_url is not None else os.environ.get("REDIS_URL", "")
    if url:
        return RedisEventBus(url)
    print("[event_bus] REDIS_URL not set; using in-memory bus "
          "(events won't cross processes — NOT FOR PRODUCTION)")
    return InMemoryEventBus()
