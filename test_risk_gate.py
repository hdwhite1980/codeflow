"""Tests for risk_gate.

We test the in-memory implementation since the production Redis one
would require running Redis. The semantics are designed to be
identical — Redis just provides the cross-process delivery.

Test coverage:
  - record_decision before wait: decision is consumed on first wait
  - wait before record_decision: waiter is notified when decision lands
  - timeout: wait returns "timeout" when no decision arrives
  - invalid decision value: rejected
  - back-to-back decisions on the same key: each wait gets a fresh value
"""

from __future__ import annotations

import asyncio
import unittest

from risk_gate import (
    InMemoryRiskGate,
    iteration_gate_key,
    fix_all_gate_key,
    make_risk_gate,
)


def _run(coro):
    return asyncio.run(coro)


class TestKeyHelpers(unittest.TestCase):
    def test_iteration_gate_key_shape(self):
        self.assertEqual(
            iteration_gate_key("abc", 3), "iteration:abc:3",
        )

    def test_fix_all_gate_key_shape(self):
        self.assertEqual(
            fix_all_gate_key("xyz", 5), "fix_all:xyz:5",
        )

    def test_keys_are_distinct(self):
        """An iteration with seq=3 and a fix-all with seq=3 on the same
        project must produce different gate keys — otherwise their
        pauses would conflict."""
        self.assertNotEqual(
            iteration_gate_key("abc", 3),
            fix_all_gate_key("abc", 3),
        )


class TestInMemoryRiskGate(unittest.TestCase):
    def test_decision_recorded_before_wait(self):
        """If the user clicks proceed before the worker reaches the
        wait (very unlikely in production but possible in tests),
        the wait should pick up the queued decision immediately."""
        gate = InMemoryRiskGate()

        async def go():
            await gate.record_decision("k1", "proceed")
            result = await gate.wait_for_decision("k1", timeout=5)
            return result

        self.assertEqual(_run(go()), "proceed")

    def test_wait_then_decision(self):
        """Normal flow: worker waits, user decides later."""
        gate = InMemoryRiskGate()

        async def go():
            async def waiter():
                return await gate.wait_for_decision("k2", timeout=5)
            async def decider():
                # Small sleep to ensure waiter is parked first.
                await asyncio.sleep(0.05)
                await gate.record_decision("k2", "cancel")
            results = await asyncio.gather(waiter(), decider())
            return results[0]

        self.assertEqual(_run(go()), "cancel")

    def test_timeout(self):
        gate = InMemoryRiskGate()

        async def go():
            return await gate.wait_for_decision("ktimeout", timeout=0.1)

        self.assertEqual(_run(go()), "timeout")

    def test_invalid_decision_rejected(self):
        gate = InMemoryRiskGate()

        async def go():
            await gate.record_decision("k", "maybe")

        with self.assertRaises(ValueError):
            _run(go())

    def test_decision_is_consumed_after_wait(self):
        """After the first wait reads the decision, a second wait on
        the same key must NOT receive the stale value."""
        gate = InMemoryRiskGate()

        async def go():
            await gate.record_decision("kreuse", "proceed")
            first = await gate.wait_for_decision("kreuse", timeout=1)
            # Second wait should now time out, not return "proceed" again.
            second = await gate.wait_for_decision("kreuse", timeout=0.1)
            return first, second

        first, second = _run(go())
        self.assertEqual(first, "proceed")
        self.assertEqual(second, "timeout")

    def test_record_decision_notified_flag(self):
        """record_decision returns True when a waiter was actually
        notified; False when the decision was queued for a future wait."""
        gate = InMemoryRiskGate()

        async def go_queued():
            # No waiter; decision goes into the queue.
            return await gate.record_decision("kq1", "proceed")
        self.assertFalse(_run(go_queued()))

        gate2 = InMemoryRiskGate()

        async def go_notified():
            async def waiter():
                return await gate2.wait_for_decision("kn1", timeout=5)
            async def decider():
                await asyncio.sleep(0.05)
                return await gate2.record_decision("kn1", "proceed")
            results = await asyncio.gather(waiter(), decider())
            return results[1]
        self.assertTrue(_run(go_notified()))


class TestMakeRiskGate(unittest.TestCase):
    def test_falls_back_to_in_memory_without_redis_url(self):
        # Pass empty string explicitly to force in-memory.
        gate = make_risk_gate(redis_url="")
        self.assertIsInstance(gate, InMemoryRiskGate)


if __name__ == "__main__":
    unittest.main()
