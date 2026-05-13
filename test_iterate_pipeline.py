"""Tests for iterate_pipeline.

The plan parser and inventory loader are testable in isolation. The
end-to-end run_iteration call requires a live Anthropic client; that's
exercised in production smoke tests rather than unit tests.
"""

from __future__ import annotations

import unittest

from iterate_pipeline import (
    IterationPlan,
    _current_inventory,
    _language_of,
    _parse_plan,
)
from ledger import ArtifactKind, Tier
from ledger_memory import InMemoryLedgerStore


class TestPlanParser(unittest.TestCase):
    def setUp(self):
        self.inventory = {
            "app/main.py": "FastAPI app entrypoint",
            "app/config.py": "settings",
            "README.md": "docs",
        }

    def test_valid_plan_with_changes_and_new_files(self):
        raw = """{
          "rationale": "Adding dark mode support",
          "changes": [
            {"path": "app/config.py", "reason": "add THEME env var"}
          ],
          "new_files": [
            {"path": "app/theme.py", "purpose": "theme constants", "language": "python", "size_hint": "small"}
          ],
          "delete": []
        }"""
        plan = _parse_plan(raw, self.inventory)
        self.assertEqual(len(plan.changes), 1)
        self.assertEqual(plan.changes[0].path, "app/config.py")
        self.assertEqual(len(plan.new_files), 1)
        self.assertEqual(plan.new_files[0].path, "app/theme.py")
        self.assertEqual(plan.deletes, [])
        self.assertIn("dark mode", plan.rationale.lower())

    def test_change_to_unknown_path_dropped(self):
        raw = """{
          "rationale": "test",
          "changes": [
            {"path": "app/main.py", "reason": "valid"},
            {"path": "nonexistent.py", "reason": "invalid"}
          ],
          "new_files": [],
          "delete": []
        }"""
        plan = _parse_plan(raw, self.inventory)
        self.assertEqual(len(plan.changes), 1)
        self.assertEqual(plan.changes[0].path, "app/main.py")

    def test_new_file_path_collision_dropped(self):
        """A 'new_file' that already exists in inventory must be dropped —
        otherwise the Builder would clobber existing files."""
        raw = """{
          "rationale": "test",
          "changes": [],
          "new_files": [
            {"path": "app/main.py", "purpose": "would clobber", "language": "python", "size_hint": "small"},
            {"path": "app/new.py", "purpose": "ok", "language": "python", "size_hint": "small"}
          ],
          "delete": []
        }"""
        plan = _parse_plan(raw, self.inventory)
        self.assertEqual(len(plan.new_files), 1)
        self.assertEqual(plan.new_files[0].path, "app/new.py")

    def test_delete_unknown_path_dropped(self):
        raw = """{
          "rationale": "test",
          "changes": [],
          "new_files": [],
          "delete": ["app/main.py", "nonexistent.py"]
        }"""
        plan = _parse_plan(raw, self.inventory)
        self.assertEqual(plan.deletes, ["app/main.py"])

    def test_malformed_json_returns_empty_plan(self):
        raw = "this is not json at all { definitely not"
        plan = _parse_plan(raw, self.inventory)
        self.assertTrue(plan.is_empty())
        self.assertIn("failed", plan.rationale.lower())

    def test_strips_code_fences(self):
        """Model sometimes adds ```json fences despite instructions."""
        raw = """```json
        {
          "rationale": "ok",
          "changes": [{"path": "app/main.py", "reason": "x"}],
          "new_files": [],
          "delete": []
        }
        ```"""
        plan = _parse_plan(raw, self.inventory)
        self.assertEqual(len(plan.changes), 1)

    def test_max_files_cap_enforced(self):
        """A plan touching more than _MAX_FILES_PER_ITERATION (30) files
        gets truncated."""
        # Build 50 valid changes against a large inventory.
        large_inv = {f"f{i}.py": f"file {i}" for i in range(60)}
        changes = [{"path": f"f{i}.py", "reason": "x"} for i in range(50)]
        raw = (
            '{"rationale":"big","changes":' + str(changes).replace("'", '"') +
            ',"new_files":[],"delete":[]}'
        )
        plan = _parse_plan(raw, large_inv)
        self.assertLessEqual(
            len(plan.changes) + len(plan.new_files) + len(plan.deletes),
            30,
        )

    def test_empty_lists_is_empty_plan(self):
        raw = """{
          "rationale": "nothing to do",
          "changes": [],
          "new_files": [],
          "delete": []
        }"""
        plan = _parse_plan(raw, self.inventory)
        self.assertTrue(plan.is_empty())

    def test_missing_fields_handled_gracefully(self):
        """Plans missing some fields should still parse — empty defaults."""
        raw = '{"rationale": "minimal"}'
        plan = _parse_plan(raw, self.inventory)
        self.assertEqual(plan.changes, [])
        self.assertEqual(plan.new_files, [])
        self.assertEqual(plan.deletes, [])


class TestInventoryLoader(unittest.TestCase):
    def test_loads_current_file_entries(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        store.write_entry(
            project_id=pid, tier=Tier.GENERATION,
            artifact_kind=ArtifactKind.FILE,
            artifact_key=f"file:{pid}:app/main.py",
            body="print('hi')",
            rationale="entrypoint",
            author="test",
        )
        store.write_entry(
            project_id=pid, tier=Tier.GENERATION,
            artifact_kind=ArtifactKind.FILE,
            artifact_key=f"file:{pid}:app/config.py",
            body="X = 1",
            rationale="settings",
            author="test",
        )
        inv = _current_inventory(store, pid)
        self.assertIn("app/main.py", inv)
        self.assertIn("app/config.py", inv)
        self.assertEqual(inv["app/main.py"], "entrypoint")

    def test_skips_tombstoned_files(self):
        """A file with a 'DELETED in iteration' rationale should not
        appear in the inventory — it's been deleted."""
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        store.write_entry(
            project_id=pid, tier=Tier.GENERATION,
            artifact_kind=ArtifactKind.FILE,
            artifact_key=f"file:{pid}:old.py",
            body="# DELETED in iteration 2\n",
            rationale="DELETED in iteration 2: no longer needed.",
            author="test",
        )
        inv = _current_inventory(store, pid)
        self.assertNotIn("old.py", inv)

    def test_empty_project(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        inv = _current_inventory(store, pid)
        self.assertEqual(inv, {})


class TestLanguageDetection(unittest.TestCase):
    def test_python(self):
        self.assertEqual(_language_of("app/main.py"), "python")

    def test_typescript(self):
        self.assertEqual(_language_of("src/page.tsx"), "typescript")
        self.assertEqual(_language_of("src/util.ts"), "typescript")

    def test_markdown(self):
        self.assertEqual(_language_of("README.md"), "markdown")

    def test_yaml(self):
        self.assertEqual(_language_of("docker-compose.yml"), "yaml")

    def test_unknown(self):
        self.assertEqual(_language_of("something.weird"), "text")


class TestFullIterationFlow(unittest.TestCase):
    """End-to-end run_iteration against InMemoryLedgerStore + a fake
    Anthropic client. Catches bugs the unit tests miss — wrong Tier
    enum values, wrong ArtifactKind, body serialization issues.

    This test exists because we shipped an iteration pipeline that
    silently failed at runtime (Tier.BUILD didn't exist), and the unit
    tests didn't catch it because they only exercised the parser, not
    the actual ledger writes. End-to-end coverage with fakes is cheap
    and catches the class of bug we suffered.
    """

    def test_empty_plan_writes_started_plan_outcome(self):
        """The 'nothing to do' path must still produce all three ledger
        entries. This is the exact failure mode we saw in iteration 3."""
        import asyncio
        from iterate_pipeline import run_iteration

        class FakeClient:
            async def complete(self, prompt, *, system=None, max_tokens=4096, **kw):
                from dataclasses import dataclass

                @dataclass
                class R:
                    text: str = (
                        '{"rationale":"already done","changes":[],'
                        '"new_files":[],"delete":[]}'
                    )
                    model: str = "test"
                    input_tokens: int = 100
                    output_tokens: int = 20
                    stop_reason: str = "end_turn"

                return R()

        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        # Seed an existing file so inventory is non-empty.
        store.write_entry(
            project_id=pid, tier=Tier.GENERATION,
            artifact_kind=ArtifactKind.FILE,
            artifact_key=f"file:{pid}:app/main.py",
            body="print('hi')", rationale="entrypoint", author="seed",
        )

        outcome = asyncio.run(run_iteration(
            project_id=pid,
            iteration_prompt="add a thing",
            iteration_seq=1,
            store=store,
            client=FakeClient(),
            recorder=None,
        ))

        # All three iteration artifacts MUST exist after the call.
        artifacts = store.all_current(pid, ArtifactKind.DECISION_RECORD)
        keys = {a.artifact_key for a in artifacts}
        self.assertIn("iteration:1:started", keys)
        self.assertIn("iteration:1:plan", keys)
        self.assertIn("iteration:1:outcome", keys,
                      "Outcome record must always be written, even for "
                      "empty plans. If this fails, _write_outcome is broken "
                      "(e.g. wrong Tier or ArtifactKind).")
        self.assertEqual(outcome.changes_applied, [])
        self.assertEqual(outcome.new_files_created, [])

    def test_plan_with_change_writes_all_records(self):
        """Plan-with-changes path also writes :outcome and produces a
        regenerated file ledger entry."""
        import asyncio
        from iterate_pipeline import run_iteration

        class FakeClient:
            def __init__(self):
                self.calls = 0

            async def complete(self, prompt, *, system=None, max_tokens=4096, **kw):
                from dataclasses import dataclass
                self.calls += 1

                @dataclass
                class R:
                    text: str
                    model: str = "test"
                    input_tokens: int = 100
                    output_tokens: int = 50
                    stop_reason: str = "end_turn"

                # First call is plan; subsequent are regenerations.
                if self.calls == 1:
                    return R(text=(
                        '{"rationale":"adding health endpoint",'
                        '"changes":[{"path":"app/main.py","reason":"add /health"}],'
                        '"new_files":[],"delete":[]}'
                    ))
                # Regen response — return new file content.
                return R(text="print('new content with /health')")

        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        store.write_entry(
            project_id=pid, tier=Tier.GENERATION,
            artifact_kind=ArtifactKind.FILE,
            artifact_key=f"file:{pid}:app/main.py",
            body="print('original')", rationale="entrypoint", author="seed",
        )

        outcome = asyncio.run(run_iteration(
            project_id=pid,
            iteration_prompt="add /health",
            iteration_seq=1,
            store=store,
            client=FakeClient(),
            recorder=None,
        ))

        # All three iteration markers present.
        decision_keys = {
            a.artifact_key
            for a in store.all_current(pid, ArtifactKind.DECISION_RECORD)
        }
        self.assertIn("iteration:1:started", decision_keys)
        self.assertIn("iteration:1:plan", decision_keys)
        self.assertIn("iteration:1:outcome", decision_keys)

        # File got regenerated.
        self.assertEqual(outcome.changes_applied, ["app/main.py"])
        # And the latest file entry has the new content.
        files = store.all_current(pid, ArtifactKind.FILE)
        main_entry = next(
            f for f in files
            if f.artifact_key == f"file:{pid}:app/main.py"
        )
        blob, _ = store.get_blob(main_entry.blob_sha256)
        self.assertIn("/health", blob.decode("utf-8"))


class TestPlanIterationCall(unittest.TestCase):
    """Catches parameter-shape mismatches with AnthropicClient.complete.

    We had a bug where the plan phase passed `user=` to .complete()
    which actually takes the prompt as positional. The unit tests
    didn't catch it because they only tested the parser, not the call.
    This test fakes the client and asserts the call signature is right."""

    def test_plan_uses_positional_prompt(self):
        import asyncio
        from iterate_pipeline import _plan_iteration

        # Fake client that records how it was called.
        class FakeClient:
            def __init__(self):
                self.call_args = None
                self.call_kwargs = None

            async def complete(self, *args, **kwargs):
                self.call_args = args
                self.call_kwargs = kwargs
                # Return a minimal valid result.
                from dataclasses import dataclass

                @dataclass
                class R:
                    text: str = '{"rationale":"test","changes":[],"new_files":[],"delete":[]}'
                    model: str = "test"
                    input_tokens: int = 10
                    output_tokens: int = 5
                    stop_reason: str = "end_turn"
                return R()

        client = FakeClient()
        asyncio.run(_plan_iteration(
            client=client,
            project_id="p1",
            stage_tag="iteration:1",
            inventory={"main.py": "test"},
            iteration_prompt="do something",
            recorder=None,
        ))
        # The prompt must be passed positionally (as the first arg),
        # not as keyword "user" or "prompt".
        self.assertEqual(len(client.call_args), 1)
        self.assertIn("do something", client.call_args[0])
        # System prompt is keyword.
        self.assertIn("system", client.call_kwargs)
        # And max_tokens.
        self.assertIn("max_tokens", client.call_kwargs)


if __name__ == "__main__":
    unittest.main()
