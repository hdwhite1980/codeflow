"""Integration tests for Turn D.1 — iteration and fix-all risk hooks.

We verify the end-to-end wiring:
  - Pre-flight risk runs when risk_client is provided
  - Pre-flight risk is skipped (gracefully) when risk_client is None
  - Critical pre-flight pauses on the gate; proceed/cancel decisions
    resume the iteration accordingly
  - Post-iteration risk runs after a successful iteration
  - Risk analysis failures don't break the iteration

We use fake LLM clients and the in-memory gate so the tests run fast
and don't need Redis or a real model. The ledger entries written by
the risk hooks are inspected directly to verify they have the right
artifact keys and bodies.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from ledger import ArtifactKind, Tier
from ledger_memory import InMemoryLedgerStore
from risk_gate import InMemoryRiskGate, iteration_gate_key
from iterate_pipeline import run_iteration


def _run(coro):
    return asyncio.run(coro)


@dataclass
class _FakeResult:
    text: str
    model: str = "test-model"
    stop_reason: str = "end_turn"
    input_tokens: int = 50
    output_tokens: int = 25


class _ScriptedClient:
    """LLM client double that returns scripted responses in order.

    For iterations: plan response first, then per-file regen responses.
    For risk: scripted JSON risk assessments.

    We use distinct clients for the Builder vs the risk analyzer in tests
    so we can script their responses independently.
    """

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0
        self.last_prompts = []

    async def complete(self, prompt, *, system=None, max_tokens=4096,
                       temperature=0.2, **kw):
        self.last_prompts.append((prompt, system))
        if self.calls >= len(self.responses):
            text = '{"rationale":"empty fallback","changes":[],"new_files":[],"delete":[]}'
        else:
            text = self.responses[self.calls]
        self.calls += 1
        return _FakeResult(text=text)


def _seed_file(store, pid, path, content="x", rationale="seed"):
    store.write_entry(
        project_id=pid, tier=Tier.GENERATION,
        artifact_kind=ArtifactKind.FILE,
        artifact_key=f"file:{pid}:{path}",
        body=content, rationale=rationale, author="seed",
    )


def _risk_response(severity="low", confidence=0.7):
    """Build a JSON risk response string."""
    return (
        '{"severity":"' + severity + '",'
        '"plain_narrative":"low impact",'
        '"technical_narrative":"safe-ish",'
        '"affected_paths":["app/main.py"],'
        '"concerns":[],'
        '"suggested_sequencing":[],'
        '"confidence":' + str(confidence) + '}'
    )


# ---------------------------------------------------------------------------
# Iteration: pre/post risk wiring.
# ---------------------------------------------------------------------------

class TestIterationRiskHooks(unittest.TestCase):
    def test_no_risk_client_skips_hooks_cleanly(self):
        """When risk_client is None (default), no risk ledger entries
        are written and the iteration runs as before."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        _seed_file(store, pid, "app/main.py")

        builder = _ScriptedClient([
            '{"rationale":"add x","changes":[{"path":"app/main.py","reason":"add x"}],"new_files":[],"delete":[]}',
            "print('new')",
        ])

        outcome = _run(run_iteration(
            project_id=pid, iteration_prompt="add /metrics",
            iteration_seq=1, store=store, client=builder,
        ))

        # Iteration succeeded.
        self.assertEqual(outcome.changes_applied, ["app/main.py"])

        # NO risk records written.
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertNotIn(f"iteration:1:pre_risk", keys)
        self.assertNotIn(f"iteration:1:post_risk", keys)

    def test_pre_and_post_risk_records_written(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        _seed_file(store, pid, "app/main.py")

        builder = _ScriptedClient([
            '{"rationale":"add x","changes":[{"path":"app/main.py","reason":"add x"}],"new_files":[],"delete":[]}',
            "print('new')",
        ])
        risk = _ScriptedClient([
            _risk_response(severity="medium"),
            _risk_response(severity="low"),
        ])

        outcome = _run(run_iteration(
            project_id=pid, iteration_prompt="add /metrics",
            iteration_seq=1, store=store, client=builder,
            risk_client=risk,
        ))

        self.assertEqual(outcome.changes_applied, ["app/main.py"])

        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        # Both risk records present.
        self.assertIn(f"iteration:1:pre_risk", keys)
        self.assertIn(f"iteration:1:post_risk", keys)

    def test_critical_pre_risk_with_proceed_runs_iteration(self):
        """Critical pre-flight pauses on the gate; proceed unblocks
        the iteration and it completes normally."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        _seed_file(store, pid, "app/main.py")

        builder = _ScriptedClient([
            '{"rationale":"risky","changes":[{"path":"app/main.py","reason":"x"}],"new_files":[],"delete":[]}',
            "print('new')",
        ])
        risk = _ScriptedClient([
            _risk_response(severity="critical"),
            _risk_response(severity="low"),  # post-risk
        ])
        gate = InMemoryRiskGate()

        async def go():
            # Schedule the proceed decision to land shortly after the
            # iteration starts and pauses.
            async def confirm():
                await asyncio.sleep(0.05)
                await gate.record_decision(
                    iteration_gate_key(pid, 1), "proceed",
                )
            iteration_task = run_iteration(
                project_id=pid, iteration_prompt="risky",
                iteration_seq=1, store=store, client=builder,
                risk_client=risk, risk_gate=gate,
            )
            results = await asyncio.gather(iteration_task, confirm())
            return results[0]

        outcome = _run(go())

        # Iteration completed: file got regenerated.
        self.assertEqual(outcome.changes_applied, ["app/main.py"])
        # No cancellation record.
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertNotIn(f"iteration:1:cancelled", keys)
        self.assertIn(f"iteration:1:pre_risk", keys)
        self.assertIn(f"iteration:1:post_risk", keys)

    def test_critical_pre_risk_with_cancel_aborts_iteration(self):
        """Critical pre-flight pauses; cancel stops the iteration
        before regen, writes a cancellation record, and the file is
        unchanged."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        _seed_file(store, pid, "app/main.py", content="ORIGINAL")

        builder = _ScriptedClient([
            '{"rationale":"risky","changes":[{"path":"app/main.py","reason":"x"}],"new_files":[],"delete":[]}',
            "print('SHOULD NOT BE WRITTEN')",
        ])
        risk = _ScriptedClient([
            _risk_response(severity="critical"),
        ])
        gate = InMemoryRiskGate()

        async def go():
            async def confirm():
                await asyncio.sleep(0.05)
                await gate.record_decision(
                    iteration_gate_key(pid, 1), "cancel",
                )
            iteration_task = run_iteration(
                project_id=pid, iteration_prompt="risky",
                iteration_seq=1, store=store, client=builder,
                risk_client=risk, risk_gate=gate,
            )
            results = await asyncio.gather(iteration_task, confirm())
            return results[0]

        outcome = _run(go())

        # No files changed.
        self.assertEqual(outcome.changes_applied, [])
        # Cancellation marker written.
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertIn(f"iteration:1:cancelled", keys)
        # File content unchanged.
        file_entry = next(
            d for d in store.all_current(pid, ArtifactKind.FILE)
            if d.artifact_key == f"file:{pid}:app/main.py"
        )
        blob, _ = store.get_blob(file_entry.blob_sha256)
        self.assertEqual(blob.decode("utf-8"), "ORIGINAL")
        # No post_risk record — we never ran regen.
        self.assertNotIn(f"iteration:1:post_risk", keys)

    def test_non_critical_does_not_pause(self):
        """high/medium/low pre-flight do NOT pause. Iteration proceeds
        without any gate interaction."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        _seed_file(store, pid, "app/main.py")

        builder = _ScriptedClient([
            '{"rationale":"x","changes":[{"path":"app/main.py","reason":"y"}],"new_files":[],"delete":[]}',
            "print('new')",
        ])
        risk = _ScriptedClient([
            _risk_response(severity="high"),
            _risk_response(severity="medium"),
        ])
        gate = InMemoryRiskGate()  # gate present but should never be touched

        outcome = _run(run_iteration(
            project_id=pid, iteration_prompt="moderately risky",
            iteration_seq=1, store=store, client=builder,
            risk_client=risk, risk_gate=gate,
        ))

        # Iteration ran successfully.
        self.assertEqual(outcome.changes_applied, ["app/main.py"])
        # Gate was never used (no event keys present).
        self.assertEqual(len(gate._events), 0)
        self.assertEqual(len(gate._decisions), 0)

    def test_risk_failure_does_not_break_iteration(self):
        """If the risk LLM call fails, the iteration still completes
        successfully. We just don't get a risk record."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        _seed_file(store, pid, "app/main.py")

        builder = _ScriptedClient([
            '{"rationale":"x","changes":[{"path":"app/main.py","reason":"y"}],"new_files":[],"delete":[]}',
            "print('new')",
        ])

        class _CrashingRiskClient:
            async def complete(self, prompt, *, system=None, max_tokens=4096,
                               temperature=0.2, **kw):
                raise RuntimeError("simulated LLM failure")

        outcome = _run(run_iteration(
            project_id=pid, iteration_prompt="x",
            iteration_seq=1, store=store, client=builder,
            risk_client=_CrashingRiskClient(),
        ))

        # Iteration still succeeded.
        self.assertEqual(outcome.changes_applied, ["app/main.py"])
        # No risk records (the crash was caught).
        decisions = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {d.artifact_key for d in decisions}
        self.assertNotIn(f"iteration:1:pre_risk", keys)
        self.assertNotIn(f"iteration:1:post_risk", keys)

    def test_empty_plan_skips_both_risks(self):
        """An iteration with an empty plan (no changes/new/delete) should
        skip both pre_risk and post_risk — no point analyzing nothing."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        _seed_file(store, pid, "app/main.py")

        builder = _ScriptedClient([
            '{"rationale":"nothing to do","changes":[],"new_files":[],"delete":[]}',
        ])
        risk = _ScriptedClient([_risk_response()])  # would be used if hooks fire

        outcome = _run(run_iteration(
            project_id=pid, iteration_prompt="no-op",
            iteration_seq=1, store=store, client=builder,
            risk_client=risk,
        ))

        self.assertEqual(outcome.changes_applied, [])
        # Risk client never called (its scripted response wasn't consumed).
        self.assertEqual(risk.calls, 0)

    def test_post_risk_uses_actual_changes_not_plan(self):
        """Post-iteration risk should reason over what ACTUALLY changed,
        which may differ from the plan if some changes failed.

        Here we deliberately have the plan say two files will change but
        only one regen actually succeeds. The post-risk prompt should
        mention only the successful one."""
        store = InMemoryLedgerStore()
        pid = store.create_project("t", "t")
        _seed_file(store, pid, "app/main.py")
        _seed_file(store, pid, "app/db.py")

        builder = _ScriptedClient([
            ('{"rationale":"two files","changes":['
             '{"path":"app/main.py","reason":"x"},'
             '{"path":"app/db.py","reason":"y"}'
             '],"new_files":[],"delete":[]}'),
            "ok",  # first regen succeeds
            # second regen will error out by exhausting scripted responses
        ])

        # Builder client raising on the second regen
        class _Builder:
            def __init__(self):
                self.calls = 0
            async def complete(self, prompt, *, system=None, max_tokens=4096,
                               temperature=0.2, **kw):
                self.calls += 1
                if self.calls == 1:
                    return _FakeResult(text=(
                        '{"rationale":"two files","changes":['
                        '{"path":"app/main.py","reason":"x"},'
                        '{"path":"app/db.py","reason":"y"}'
                        '],"new_files":[],"delete":[]}'
                    ))
                if self.calls == 2:
                    return _FakeResult(text="print('main updated')")
                # Third call (second regen) raises.
                raise RuntimeError("simulated network error")

        risk = _ScriptedClient([
            _risk_response(severity="low"),    # pre
            _risk_response(severity="low"),    # post
        ])

        outcome = _run(run_iteration(
            project_id=pid, iteration_prompt="two files",
            iteration_seq=1, store=store, client=_Builder(),
            risk_client=risk,
        ))

        # Only one file actually changed.
        self.assertEqual(outcome.changes_applied, ["app/main.py"])
        self.assertEqual(len(outcome.failed), 1)

        # The post-risk prompt (the second call to risk client) should
        # mention "app/main.py" but NOT "app/db.py" (since db.py didn't
        # actually change).
        post_prompt = risk.last_prompts[1][0]
        self.assertIn("app/main.py", post_prompt)
        # Look specifically in the change-description section, not the
        # full project inventory.
        # Find the "Files regenerated:" line if present.
        regen_lines = [
            line for line in post_prompt.splitlines()
            if line.startswith("Files regenerated:") or line.startswith("New files created:")
        ]
        joined = " ".join(regen_lines)
        self.assertNotIn("app/db.py", joined)


if __name__ == "__main__":
    unittest.main()
