"""
codeflow.publishing_store
=========================

Thin wrapper around LedgerStore + UsageRecorder that publishes events
to an EventBus after every write.

Why a wrapper instead of modifying LedgerStore directly
--------------------------------------------------------
LedgerStore is used by many code paths (build pipeline, audit pipeline,
runtime sync, tests) — most of which don't need event publishing. Adding
optional `event_bus: Optional[EventBus]` everywhere would create a lot
of plumbing for little benefit. Instead, we wrap the store at the layer
where the worker constructs it. The pipelines see a LedgerStore-shaped
object and don't know they're being observed.

We forward every non-mutating method via __getattr__, and intercept
write_entry() to publish a "ledger_entry" event after the underlying
write commits. Same pattern for UsageRecorder.record().

Event shape
-----------
Each event has:
  - kind: "ledger_entry" | "usage_row"
  - project_id: the project the event is about
  - data: a dict matching what the REST endpoints would return for
    this row, so the frontend can integrate it into the same state
    shape as the initial REST fetch.

This means the frontend's reducer doesn't need to know whether a row
came from REST or WS — it merges them by key.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from event_bus import EventBus
from ledger import (
    ArtifactKind, EdgeKind, LedgerEntry, LedgerStore, Tier,
)
from usage_recorder import UsageRecorder


def _now_iso() -> str:
    return datetime.now(tz=timezone.utc).isoformat()


def _schedule_publish(bus: EventBus, project_id: str, event: dict[str, Any]) -> None:
    """Fire-and-forget publish. We don't await because the caller (the
    pipeline) is synchronous from the worker's perspective at the
    write_entry boundary, and we don't want to slow it down on a
    bus that might be lagging.

    We schedule the coroutine on the running event loop. If there's no
    running loop (i.e. in synchronous test code), we log and skip — the
    REST endpoints still work, the frontend just won't see the live
    event."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No running loop. This happens in sync test code; not an error.
        return
    loop.create_task(bus.publish(project_id, event))


class PublishingLedgerStore:
    """Wraps a LedgerStore. Forwards everything; publishes on write_entry.

    Duck-typed to LedgerStore — anywhere we used to pass a LedgerStore,
    we can pass one of these. The pipelines call .write_entry and
    .get_blob and .all_current; all of those work transparently."""

    def __init__(self, store: LedgerStore, bus: EventBus) -> None:
        self._store = store
        self._bus = bus

    def write_entry(
        self,
        project_id: str,
        tier: Tier,
        artifact_kind: ArtifactKind,
        artifact_key: str,
        body: Any,
        rationale: str,
        author: str,
        **kwargs: Any,
    ) -> LedgerEntry:
        entry = self._store.write_entry(
            project_id=project_id,
            tier=tier,
            artifact_kind=artifact_kind,
            artifact_key=artifact_key,
            body=body,
            rationale=rationale,
            author=author,
            **kwargs,
        )
        # Build the event payload. We mirror what /api/projects/<id>/artifacts
        # returns per row, so the frontend can integrate without special
        # cases. body is intentionally NOT included — bodies can be large
        # (whole file contents), and the frontend can fetch them on demand
        # via /audits or other endpoints when it wants the detail.
        event = {
            "kind": "ledger_entry",
            "project_id": project_id,
            "data": {
                "artifact_key": entry.artifact_key,
                "kind": entry.artifact_kind.value,
                "tier": entry.tier.value,
                "rationale": entry.rationale,
                "author": entry.author,
                "seq": entry.seq,
                "created_at": _now_iso(),
            },
        }
        _schedule_publish(self._bus, project_id, event)
        return entry

    # Forward everything else to the underlying store. __getattr__ kicks
    # in only when the attribute isn't found on PublishingLedgerStore
    # itself, so we don't accidentally intercept the wrapped methods.
    def __getattr__(self, name: str) -> Any:
        return getattr(self._store, name)


class PublishingUsageRecorder:
    """Wraps a UsageRecorder. Forwards everything; publishes on record.

    Same pattern as PublishingLedgerStore. The cost ticker on the
    frontend depends on these events landing per API call."""

    def __init__(self, recorder: UsageRecorder, bus: EventBus) -> None:
        self._recorder = recorder
        self._bus = bus

    def record(
        self,
        *,
        project_id: str,
        provider: str,
        model: str,
        stage: str,
        subject: Optional[str],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        # Underlying recorders return None — they're write-only. We compute
        # cost the same way they do (via pricing.compute_costs) so the
        # frontend's running cost ticker stays consistent with what the
        # DB-side row will show when summarize() reads it later.
        from pricing import compute_costs
        input_cost, output_cost = compute_costs(model, input_tokens, output_tokens)
        self._recorder.record(
            project_id=project_id, provider=provider, model=model,
            stage=stage, subject=subject,
            input_tokens=input_tokens, output_tokens=output_tokens,
        )
        event = {
            "kind": "usage_row",
            "project_id": project_id,
            "data": {
                "provider": provider,
                "model": model,
                "stage": stage,
                "subject": subject,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "input_cost_usd": float(input_cost),
                "output_cost_usd": float(output_cost),
                "total_tokens": input_tokens + output_tokens,
                "total_cost_usd": float(input_cost + output_cost),
            },
        }
        _schedule_publish(self._bus, project_id, event)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._recorder, name)
