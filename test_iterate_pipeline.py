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


if __name__ == "__main__":
    unittest.main()
