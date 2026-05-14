"""
codeflow.stop_flag
==================

Cooperative cancellation primitive for long-running worker jobs.

The worker checks `is_set(key)` at stage boundaries during fix-all
(and potentially other long-running flows). The API sets the flag
via `request_stop(key)`. When the worker sees the flag, it bails
gracefully and writes a `:stopped` marker to the ledger.

This is distinct from risk_gate:
  - risk_gate is BLOCKING — the worker waits for a decision
  - stop_flag is POLLING — the worker checks between stages

Same backing pattern: Redis for production cross-process delivery,
in-memory for tests.
"""

from __future__ import annotations

import os
from typing import Optional


# How long stop requests live. If a user requests stop but the worker
# never sees it (job already complete, worker crashed, etc.), the flag
# is cleaned up after this TTL. Generous because a fix-all can run for
# 5+ minutes; we want the flag to survive that.
DEFAULT_TTL_SECONDS = 1800  # 30 minutes


class StopFlag:
    """Abstract base. All methods are async because the Redis backend
    requires it; the in-memory backend just complies for interface
    uniformity."""

    async def request_stop(self, key: str) -> bool:
        """Mark this key as stop-requested. Returns True on success.
        Idempotent — calling twice is harmless."""
        raise NotImplementedError

    async def is_set(self, key: str) -> bool:
        """Check whether stop was requested for this key. Workers call
        this at stage boundaries."""
        raise NotImplementedError

    async def clear(self, key: str) -> None:
        """Remove the flag. Workers should call this on graceful
        completion so any retry doesn't see a stale stop request."""
        raise NotImplementedError


class RedisStopFlag(StopFlag):
    """Production backend. Uses Redis SET / GET / DEL with a TTL."""

    def __init__(self, redis_url: str) -> None:
        if not redis_url:
            raise ValueError("RedisStopFlag requires a non-empty REDIS_URL")
        import redis.asyncio as redis_async
        self._client = redis_async.from_url(redis_url, decode_responses=True)

    def _redis_key(self, key: str) -> str:
        return f"codeflow:stop_flag:{key}"

    async def request_stop(self, key: str) -> bool:
        try:
            await self._client.set(
                self._redis_key(key), "1", ex=DEFAULT_TTL_SECONDS,
            )
            return True
        except Exception as exc:
            print(f"[stop_flag] request_stop failed for {key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return False

    async def is_set(self, key: str) -> bool:
        try:
            v = await self._client.get(self._redis_key(key))
            return bool(v)
        except Exception as exc:
            print(f"[stop_flag] is_set check failed for {key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return False

    async def clear(self, key: str) -> None:
        try:
            await self._client.delete(self._redis_key(key))
        except Exception as exc:
            print(f"[stop_flag] clear failed for {key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass


class InMemoryStopFlag(StopFlag):
    """Test backend. Holds flags in a process-local dict."""

    def __init__(self) -> None:
        self._flags: set[str] = set()

    async def request_stop(self, key: str) -> bool:
        self._flags.add(key)
        return True

    async def is_set(self, key: str) -> bool:
        return key in self._flags

    async def clear(self, key: str) -> None:
        self._flags.discard(key)


def make_stop_flag(redis_url: Optional[str] = None) -> StopFlag:
    """Factory mirroring make_risk_gate. Returns Redis-backed when
    REDIS_URL is configured; in-memory otherwise (NOT for production)."""
    url = redis_url if redis_url is not None else os.environ.get("REDIS_URL", "")
    if url:
        return RedisStopFlag(url)
    print("[stop_flag] REDIS_URL not set; using in-memory flag "
          "(stop requests won't cross processes — NOT FOR PRODUCTION)")
    return InMemoryStopFlag()


# Key convention — matches risk_gate's iteration/fix_all key shapes.
def fix_all_stop_key(project_id: str, seq: int) -> str:
    return f"fix_all:{project_id}:{seq}"
