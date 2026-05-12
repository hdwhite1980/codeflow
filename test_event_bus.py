"""Tests for event_bus + publishing_store: pub/sub semantics, fire-and-forget
publishing on ledger and usage writes, and that wrapping is transparent
(other LedgerStore methods still work)."""

from __future__ import annotations

import asyncio
import unittest

from event_bus import InMemoryEventBus
from ledger import ArtifactKind, Tier
from ledger_memory import InMemoryLedgerStore
from publishing_store import PublishingLedgerStore, PublishingUsageRecorder
from usage_recorder import InMemoryUsageRecorder


def _run(coro):
    return asyncio.run(coro)


class TestInMemoryEventBus(unittest.TestCase):
    def test_publish_records_event(self):
        bus = InMemoryEventBus()
        _run(bus.publish("proj-1", {"kind": "test", "n": 1}))
        self.assertEqual(len(bus.published), 1)
        pid, ev = bus.published[0]
        self.assertEqual(pid, "proj-1")
        self.assertEqual(ev["n"], 1)

    def test_subscribe_receives_published_events(self):
        bus = InMemoryEventBus()

        async def reader():
            received = []
            async for ev in bus.subscribe("proj-1"):
                received.append(ev)
                if len(received) >= 2:
                    break
            return received

        async def writer():
            await asyncio.sleep(0.01)
            await bus.publish("proj-1", {"kind": "one"})
            await bus.publish("proj-1", {"kind": "two"})

        async def both():
            return await asyncio.gather(reader(), writer())

        results, _ = _run(both())
        self.assertEqual([e["kind"] for e in results], ["one", "two"])

    def test_subscribe_isolated_by_project(self):
        bus = InMemoryEventBus()

        async def reader():
            received = []
            async for ev in bus.subscribe("proj-A"):
                received.append(ev)
                break
            return received

        async def writer():
            await asyncio.sleep(0.01)
            # Publish to a different project — A's subscriber should not see it.
            await bus.publish("proj-B", {"kind": "wrong"})
            await asyncio.sleep(0.01)
            # Now publish to A — this should land.
            await bus.publish("proj-A", {"kind": "right"})

        async def both():
            return await asyncio.gather(reader(), writer())

        results, _ = _run(both())
        self.assertEqual(results[0]["kind"], "right")


class TestPublishingLedgerStore(unittest.TestCase):
    def test_write_entry_publishes_ledger_event(self):
        bus = InMemoryEventBus()
        bare = InMemoryLedgerStore()
        store = PublishingLedgerStore(bare, bus)
        pid = bare.create_project("test", "build something")

        async def go():
            store.write_entry(
                project_id=pid,
                tier=Tier.GENERATION,
                artifact_kind=ArtifactKind.FILE,
                artifact_key=f"file:{pid}:main.py",
                body="print(1)\n",
                rationale="test write",
                author="test",
            )
            # Give the scheduled publish task a chance to run.
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        _run(go())

        # The event should be on the bus.
        self.assertEqual(len(bus.published), 1)
        pid_out, ev = bus.published[0]
        self.assertEqual(pid_out, pid)
        self.assertEqual(ev["kind"], "ledger_entry")
        self.assertEqual(ev["data"]["kind"], "file")
        self.assertEqual(ev["data"]["artifact_key"], f"file:{pid}:main.py")
        # body deliberately not included — frontend fetches on demand.
        self.assertNotIn("body", ev["data"])

    def test_other_methods_transparently_forwarded(self):
        """all_current, get_blob, etc. should keep working through the wrapper."""
        bus = InMemoryEventBus()
        bare = InMemoryLedgerStore()
        store = PublishingLedgerStore(bare, bus)
        pid = bare.create_project("test", "build something")

        async def go():
            entry = store.write_entry(
                project_id=pid, tier=Tier.GENERATION,
                artifact_kind=ArtifactKind.FILE,
                artifact_key=f"file:{pid}:a.py",
                body="x = 1",
                rationale="test", author="test",
            )
            await asyncio.sleep(0)
            return entry

        entry = _run(go())

        # Forwarded reads work.
        entries = store.all_current(pid, ArtifactKind.FILE)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].artifact_key, f"file:{pid}:a.py")

        blob, content_type = store.get_blob(entry.blob_sha256)
        self.assertEqual(blob, b"x = 1")


class TestPublishingUsageRecorder(unittest.TestCase):
    def test_record_publishes_usage_event(self):
        bus = InMemoryEventBus()
        bare = InMemoryUsageRecorder()
        rec = PublishingUsageRecorder(bare, bus)

        async def go():
            rec.record(
                project_id="proj-X",
                provider="anthropic",
                model="claude-sonnet-4-6",
                stage="file",
                subject="main.py",
                input_tokens=100,
                output_tokens=200,
            )
            await asyncio.sleep(0)
            await asyncio.sleep(0)

        _run(go())

        self.assertEqual(len(bus.published), 1)
        pid, ev = bus.published[0]
        self.assertEqual(pid, "proj-X")
        self.assertEqual(ev["kind"], "usage_row")
        self.assertEqual(ev["data"]["provider"], "anthropic")
        self.assertEqual(ev["data"]["stage"], "file")
        self.assertEqual(ev["data"]["input_tokens"], 100)
        self.assertEqual(ev["data"]["output_tokens"], 200)
        # Cost fields should be present and reflect the model's pricing.
        self.assertIn("total_cost_usd", ev["data"])
        self.assertGreater(ev["data"]["total_cost_usd"], 0)


class TestPublishingDoesNotBlock(unittest.TestCase):
    """Sanity check: if the bus is slow or fails, the underlying write
    must still succeed. The whole point of fire-and-forget."""

    def test_bus_failure_does_not_break_write(self):

        class ExplodingBus(InMemoryEventBus):
            async def publish(self, project_id, event):
                raise RuntimeError("bus is on fire")

        bus = ExplodingBus()
        bare = InMemoryLedgerStore()
        store = PublishingLedgerStore(bare, bus)
        pid = bare.create_project("ok", "ok")

        async def go():
            # The write should succeed even if the publish task raises.
            entry = store.write_entry(
                project_id=pid, tier=Tier.GENERATION,
                artifact_kind=ArtifactKind.FILE,
                artifact_key=f"file:{pid}:a.py",
                body="content",
                rationale="ok", author="ok",
            )
            # Let the doomed publish task run and die.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            return entry

        entry = _run(go())
        # The entry made it into the underlying store.
        self.assertIsNotNone(entry)
        self.assertEqual(len(bare.all_current(pid, ArtifactKind.FILE)), 1)


if __name__ == "__main__":
    unittest.main()
