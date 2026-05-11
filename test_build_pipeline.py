"""
test_build_pipeline
===================

Exercises run_build with a fake AnthropicClient that returns scripted
responses. Covers the happy path, JSON parse failures, schema validation
failures, retry logic, partial file generation failures, and missing-key
behavior at the handler level.

We do NOT call the real Anthropic API in tests. Every CompletionResult
the fake returns is hand-built and asserted against. That keeps tests
hermetic, fast, and free.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Optional

from anthropic_client import AnthropicError, CompletionResult
from build_pipeline import (
    MAX_FILES_PER_PROJECT, BuildOutcome,
    _extract_json, _parse_spec, _validate_path, run_build,
    spec_manifest_key, spec_file_item_key, file_artifact_key,
)
from job_handlers import HandlerContext, dispatch
from jobqueue import make_job
from ledger_memory import InMemoryLedgerStore


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake AnthropicClient. Replays canned responses; records every call.
# ---------------------------------------------------------------------------

class FakeAnthropicClient:
    """Mimics AnthropicClient.complete() with a queue of canned responses.

    Each canned response is either a string (treated as text with default
    token counts) or a CompletionResult. Calls that exhaust the queue
    raise AssertionError so missing scripts fail loud instead of silently
    returning empty strings.

    Exceptions can be queued too — push an AnthropicError instance and it
    will be raised on the next call.
    """

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(
        self, prompt, *, system=None, model=None,
        max_tokens=4096, temperature=0.2,
    ):
        self.calls.append({
            "prompt": prompt,
            "system": system,
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
        })
        if not self._responses:
            raise AssertionError(
                f"FakeAnthropicClient ran out of scripted responses; "
                f"call #{len(self.calls)} had no script."
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, CompletionResult):
            return nxt
        # Treat plain strings as text with token counts of 100/200.
        return CompletionResult(
            text=nxt, model="fake", stop_reason="end_turn",
            input_tokens=100, output_tokens=200,
        )

    async def aclose(self):
        pass


# Convenience builder for valid spec JSON.
def make_spec_json(*files):
    return json.dumps({
        "summary": "a test project",
        "files": [
            {
                "path": f["path"],
                "purpose": f.get("purpose", "do something useful"),
                "language": f.get("language", "python"),
                "imports": f.get("imports", []),
                "size_hint": f.get("size_hint", "small"),
            }
            for f in files
        ],
    })


# ---------------------------------------------------------------------------
# Helper tests for parsers and validators.
# ---------------------------------------------------------------------------

class TestParsers(unittest.TestCase):
    def test_extract_json_plain(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_extract_json_with_prose(self):
        text = 'Here is your JSON:\n\n```json\n{"x": 2}\n```\n\nHope that helps.'
        self.assertEqual(_extract_json(text), {"x": 2})

    def test_extract_json_returns_none_on_garbage(self):
        self.assertIsNone(_extract_json("not even close to JSON"))

    def test_validate_path_accepts_normal(self):
        self.assertTrue(_validate_path("src/app.py"))
        self.assertTrue(_validate_path("README.md"))

    def test_validate_path_rejects_traversal(self):
        self.assertFalse(_validate_path("../etc/passwd"))
        self.assertFalse(_validate_path("a/../b"))

    def test_validate_path_rejects_absolute(self):
        self.assertFalse(_validate_path("/etc/hosts"))

    def test_validate_path_rejects_backslash(self):
        self.assertFalse(_validate_path("src\\app.py"))

    def test_parse_spec_happy_path(self):
        raw = {
            "summary": "x",
            "files": [
                {"path": "a.py", "purpose": "p", "language": "python",
                 "imports": [], "size_hint": "small"},
            ],
        }
        spec = _parse_spec(raw)
        self.assertIsNotNone(spec)
        self.assertEqual(len(spec.files), 1)
        self.assertEqual(spec.files[0].path, "a.py")

    def test_parse_spec_rejects_zero_files(self):
        self.assertIsNone(_parse_spec({"summary": "x", "files": []}))

    def test_parse_spec_rejects_duplicate_paths(self):
        raw = {
            "summary": "x",
            "files": [
                {"path": "a.py", "purpose": "p", "language": "python", "imports": []},
                {"path": "a.py", "purpose": "q", "language": "python", "imports": []},
            ],
        }
        self.assertIsNone(_parse_spec(raw))

    def test_parse_spec_rejects_traversal_path(self):
        raw = {
            "summary": "x",
            "files": [
                {"path": "../escape.py", "purpose": "p", "language": "python", "imports": []},
            ],
        }
        self.assertIsNone(_parse_spec(raw))

    def test_parse_spec_defaults_size_hint_when_unknown(self):
        raw = {
            "summary": "x",
            "files": [
                {"path": "a.py", "purpose": "p", "language": "python",
                 "imports": [], "size_hint": "ginormous"},
            ],
        }
        spec = _parse_spec(raw)
        self.assertEqual(spec.files[0].size_hint, "medium")


# ---------------------------------------------------------------------------
# Pipeline tests.
# ---------------------------------------------------------------------------

class TestRunBuildHappyPath(unittest.TestCase):
    def test_two_file_project_writes_spec_and_files(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("demo", "build me a hello world")
        client = FakeAnthropicClient([
            make_spec_json(
                {"path": "app.py", "purpose": "entry", "size_hint": "small"},
                {"path": "README.md", "purpose": "docs", "language": "markdown",
                 "size_hint": "small", "imports": ["app.py"]},
            ),
            "print('hello world')\n",
            "# Hello\n\nThis is a hello world.\n",
        ])

        outcome = _run(run_build(
            project_id=pid, prompt="hello world",
            client=client, store=store,
        ))

        self.assertTrue(outcome.succeeded)
        self.assertEqual(set(outcome.files_written), {"app.py", "README.md"})
        self.assertEqual(outcome.files_failed, [])
        self.assertEqual(len(client.calls), 3)  # 1 spec + 2 files

        # Ledger should have:
        #  - 1 manifest entry
        #  - 2 per-file spec entries
        #  - 2 file artifacts
        entries = store.all_current(pid)
        keys = {e.artifact_key for e in entries}
        self.assertIn(spec_manifest_key(pid), keys)
        self.assertIn(spec_file_item_key(pid, "app.py"), keys)
        self.assertIn(spec_file_item_key(pid, "README.md"), keys)
        self.assertIn(file_artifact_key(pid, "app.py"), keys)
        self.assertIn(file_artifact_key(pid, "README.md"), keys)

    def test_spec_lower_temperature_than_files(self):
        """The spec call should use a stricter temperature than the file
        calls. Pin this so future tweaks don't accidentally flip it."""
        store = InMemoryLedgerStore()
        pid = store.create_project("demo", "x" * 12)
        client = FakeAnthropicClient([
            make_spec_json({"path": "a.py"}),
            "print(1)\n",
        ])
        _run(run_build(project_id=pid, prompt="x" * 12, client=client, store=store))
        spec_temp = client.calls[0]["temperature"]
        file_temp = client.calls[1]["temperature"]
        self.assertLess(spec_temp, file_temp)


class TestRunBuildSpecFailures(unittest.TestCase):
    def test_invalid_json_retried_once_then_gives_up(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("demo", "build something")
        client = FakeAnthropicClient([
            "this is not JSON",
            "still not JSON",
        ])
        outcome = _run(run_build(
            project_id=pid, prompt="build something",
            client=client, store=store,
        ))
        self.assertFalse(outcome.succeeded)
        self.assertIsNone(outcome.spec)
        # Should have tried exactly 2 times (1 + 1 retry).
        self.assertEqual(len(client.calls), 2)
        # Token counters reflect the wasted calls — important for
        # accurate cost tracking even on failure.
        self.assertGreater(outcome.total_input_tokens, 0)
        self.assertGreater(outcome.total_output_tokens, 0)

    def test_valid_json_but_invalid_schema_retried(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("demo", "build something")
        client = FakeAnthropicClient([
            # Valid JSON, missing "files" key.
            '{"summary": "thing"}',
            # Still invalid on second try.
            '{"summary": "thing", "files": []}',
        ])
        outcome = _run(run_build(
            project_id=pid, prompt="build something",
            client=client, store=store,
        ))
        self.assertFalse(outcome.succeeded)
        self.assertIsNone(outcome.spec)

    def test_first_garbage_then_valid_succeeds(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("demo", "build something")
        client = FakeAnthropicClient([
            "garbage on first call",
            make_spec_json({"path": "a.py"}),
            "print(1)\n",
        ])
        outcome = _run(run_build(
            project_id=pid, prompt="build something",
            client=client, store=store,
        ))
        self.assertTrue(outcome.succeeded)
        self.assertEqual(outcome.files_written, ["a.py"])


class TestRunBuildFilePartialFailure(unittest.TestCase):
    def test_one_file_fails_others_still_written(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("demo", "build something")
        client = FakeAnthropicClient([
            make_spec_json(
                {"path": "a.py"},
                {"path": "b.py"},
                {"path": "c.py"},
            ),
            "content for a\n",
            AnthropicError(500, "internal error"),
            "content for c\n",
        ])
        outcome = _run(run_build(
            project_id=pid, prompt="build something",
            client=client, store=store,
        ))
        self.assertFalse(outcome.succeeded)
        self.assertEqual(set(outcome.files_written), {"a.py", "c.py"})
        self.assertEqual(len(outcome.files_failed), 1)
        self.assertEqual(outcome.files_failed[0][0], "b.py")

    def test_auth_error_aborts_remaining_files(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("demo", "build something")
        client = FakeAnthropicClient([
            make_spec_json(
                {"path": "a.py"}, {"path": "b.py"}, {"path": "c.py"},
            ),
            AnthropicError(401, "bad key"),
            # Should never be called — the loop should stop after 401.
        ])
        outcome = _run(run_build(
            project_id=pid, prompt="build something",
            client=client, store=store,
        ))
        self.assertEqual(outcome.files_written, [])
        # All three files end up in failures: the first as the actual
        # 401, the rest as "we bailed before trying."
        # Current behavior is to break out of the loop entirely, so b.py
        # and c.py are not even attempted. The outcome must reflect that.
        self.assertEqual(len(outcome.files_failed), 1)
        self.assertEqual(outcome.files_failed[0][0], "a.py")
        # And we should have made exactly 2 calls (1 spec + 1 failed file).
        self.assertEqual(len(client.calls), 2)


# ---------------------------------------------------------------------------
# Handler-level tests through the dispatcher.
# ---------------------------------------------------------------------------

class TestHandlerBuildProjectNoKey(unittest.TestCase):
    def test_missing_anthropic_client_writes_skip_record(self):
        async def go():
            store = InMemoryLedgerStore()
            pid = store.create_project("nokey", "build me a thing please")
            ctx = HandlerContext(store=store, anthropic=None)
            await dispatch(
                make_job("build_project", project_id=pid, prompt="build me a thing"),
                ctx,
            )
            return pid, store

        pid, store = _run(go())
        entries = store.all_current(pid)
        keys = {e.artifact_key for e in entries}
        # Skip record should have landed; no started/outcome.
        self.assertIn(f"build:{pid}:skipped", keys)
        self.assertNotIn(f"build:{pid}:started", keys)


class TestHandlerBuildProjectHappy(unittest.TestCase):
    def test_handler_writes_started_and_outcome_around_pipeline(self):
        async def go():
            store = InMemoryLedgerStore()
            pid = store.create_project("happy", "build me a thing please")
            client = FakeAnthropicClient([
                make_spec_json({"path": "main.py"}),
                "print('hi')\n",
            ])
            ctx = HandlerContext(store=store, anthropic=client)
            await dispatch(
                make_job("build_project", project_id=pid, prompt="build me a thing"),
                ctx,
            )
            return pid, store

        pid, store = _run(go())
        entries = store.all_current(pid)
        keys = {e.artifact_key for e in entries}
        # Both bookend records present.
        self.assertIn(f"build:{pid}:started", keys)
        self.assertIn(f"build:{pid}:outcome", keys)
        # File artifact present.
        self.assertIn(file_artifact_key(pid, "main.py"), keys)
        # Spec manifest present.
        self.assertIn(spec_manifest_key(pid), keys)


if __name__ == "__main__":
    unittest.main()
