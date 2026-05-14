"""
codeflow.risk_gate
==================

Pause/resume coordination for iterations and fix-all passes blocked
by a critical pre-flight risk assessment.

When pre-flight produces a `critical` severity, the worker calls
`wait_for_decision(key)` which blocks until either:
  - the user submits a proceed/cancel decision via API (which calls
    `record_decision(key, "proceed")` or `record_decision(key, "cancel")`)
  - the timeout expires (default 1 hour) → auto-cancel

Backed by Redis when REDIS_URL is set (so the web service can record
a decision in one process while the worker waits in another). Falls
back to an in-memory primitive for tests and local dev.

Design notes
------------
- Each gate has a unique key (e.g. `iteration:<pid>:<seq>` or
  `fix_all:<pid>:<seq>`). The worker creates the gate when it pauses;
  the web service records the decision by setting the gate's value.
- We use Redis's BLPOP for the wait — it's a blocking left-pop on a list.
  The web service does LPUSH with "proceed" or "cancel"; the worker's
  BLPOP wakes up with that value. After processing, we delete the key
  so a re-pause on the same seq starts fresh.
- For the in-memory case (tests), we use asyncio.Event + a shared dict.
"""

from __future__ import annotations

import asyncio
import os
from typing import Optional


# How long to wait for a user decision before auto-cancelling.
# Production-tunable via env. Long enough that users can leave the page
# and come back; short enough that abandoned iterations don't linger
# forever.
DEFAULT_TIMEOUT_SECONDS = float(os.environ.get("RISK_GATE_TIMEOUT", "3600"))


class RiskGate:
    """Abstract base."""

    async def wait_for_decision(
        self, key: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        """Block until a decision is recorded for `key`. Returns
        "proceed", "cancel", or "timeout".

        Removes the gate entry after reading so subsequent waits on
        the same key don't pick up stale decisions.
        """
        raise NotImplementedError

    async def record_decision(self, key: str, decision: str) -> bool:
        """Record a proceed/cancel decision for the gate. Returns True
        if a worker was waiting and has been notified; False if no
        wait was active (decision is queued for the next waiter, OR
        ignored depending on implementation).
        """
        raise NotImplementedError


class RedisRiskGate(RiskGate):
    """Production backend. One Redis list per gate; BLPOP from the
    waiter side, LPUSH from the decision side. After the wait
    completes (or times out) we DEL the key for clean state.

    Lazy-imports redis so non-production code paths don't pull it in.
    """

    def __init__(self, redis_url: str) -> None:
        if not redis_url:
            raise ValueError("RedisRiskGate requires a non-empty REDIS_URL")
        import redis.asyncio as redis_async
        self._redis_async = redis_async
        self._url = redis_url
        # One pooled client per gate instance. Connections are pooled
        # internally so this is cheap.
        self._client = redis_async.from_url(redis_url, decode_responses=True)

    def _redis_key(self, key: str) -> str:
        # Namespace separate from other Redis usage in the project.
        return f"codeflow:risk_gate:{key}"

    async def wait_for_decision(
        self, key: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        rkey = self._redis_key(key)
        try:
            # BLPOP returns (key, value) on success, None on timeout.
            # Use int(timeout) because BLPOP requires int seconds.
            result = await self._client.blpop(rkey, timeout=int(timeout))
            if result is None:
                # Timed out. Make sure the key is clean for any retry.
                await self._client.delete(rkey)
                return "timeout"
            _, value = result
            # Clean up after consuming, in case a second decision was
            # queued (race protection).
            await self._client.delete(rkey)
            return value if value in ("proceed", "cancel") else "cancel"
        except Exception as exc:
            print(f"[risk_gate] wait failed for {key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return "cancel"

    async def record_decision(self, key: str, decision: str) -> bool:
        if decision not in ("proceed", "cancel"):
            raise ValueError(f"decision must be 'proceed' or 'cancel', got {decision!r}")
        rkey = self._redis_key(key)
        try:
            # LPUSH wakes any BLPOP waiter. We don't know whether a
            # waiter is active — Redis doesn't tell us. Setting a TTL
            # on the key cleans it up if no one was waiting.
            await self._client.lpush(rkey, decision)
            await self._client.expire(rkey, int(DEFAULT_TIMEOUT_SECONDS))
            return True
        except Exception as exc:
            print(f"[risk_gate] record_decision failed for {key}: "
                  f"{type(exc).__name__}: {exc}", flush=True)
            return False

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            pass


class InMemoryRiskGate(RiskGate):
    """Test backend. Holds decisions in a dict keyed by gate key.
    Waiters await an asyncio.Event that gets set when a decision
    arrives. Not for production — no cross-process delivery."""

    def __init__(self) -> None:
        self._decisions: dict[str, str] = {}
        self._events: dict[str, asyncio.Event] = {}

    def _event_for(self, key: str) -> asyncio.Event:
        if key not in self._events:
            self._events[key] = asyncio.Event()
        return self._events[key]

    async def wait_for_decision(
        self, key: str, *, timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        # If a decision was already recorded (race: API called before
        # worker reached the wait), return it immediately.
        if key in self._decisions:
            value = self._decisions.pop(key)
            return value
        event = self._event_for(key)
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            self._events.pop(key, None)
            return "timeout"
        value = self._decisions.pop(key, "cancel")
        self._events.pop(key, None)
        return value

    async def record_decision(self, key: str, decision: str) -> bool:
        if decision not in ("proceed", "cancel"):
            raise ValueError(f"decision must be 'proceed' or 'cancel', got {decision!r}")
        self._decisions[key] = decision
        if key in self._events:
            self._events[key].set()
            return True
        return False


def make_risk_gate(redis_url: Optional[str] = None) -> RiskGate:
    """Factory. Same pattern as make_queue / make_event_bus elsewhere
    in the codebase. Returns a Redis-backed gate in production; an
    in-memory gate when REDIS_URL is absent."""
    url = redis_url if redis_url is not None else os.environ.get("REDIS_URL", "")
    if url:
        return RedisRiskGate(url)
    print("[risk_gate] REDIS_URL not set; using in-memory gate "
          "(decisions won't cross processes — NOT FOR PRODUCTION)")
    return InMemoryRiskGate()


# Gate key conventions — exported so callers don't reinvent.
def iteration_gate_key(project_id: str, seq: int) -> str:
    return f"iteration:{project_id}:{seq}"


def fix_all_gate_key(project_id: str, seq: int) -> str:
    return f"fix_all:{project_id}:{seq}"
