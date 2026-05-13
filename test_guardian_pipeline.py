"""Tests for guardian_pipeline (Turn A — file-grained semantic indexing).

We test the pure functions (should_index, _parse_summary_json,
_coerce_string_list) directly. The summarize_file call is tested against
a fake OllamaClient that returns a controlled response, so we exercise
the prompt building, the LLM response parsing, and the FileSummary
construction without hitting a real model.

Ledger write + read round-trip is tested against InMemoryLedgerStore.
"""

from __future__ import annotations

import asyncio
import unittest
from dataclasses import dataclass

from guardian_pipeline import (
    FileSummary,
    MAX_INDEXABLE_BYTES,
    _coerce_string_list,
    _parse_summary_json,
    load_file_summaries,
    should_index,
    summarize_file,
    write_file_summary,
)
from ledger import ArtifactKind, Tier
from ledger_memory import InMemoryLedgerStore


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Fake OllamaClient for the summarize_file path tests.
# ---------------------------------------------------------------------------

@dataclass
class _FakeResult:
    text: str
    model: str = "qwen2.5-coder:14b"
    stop_reason: str = "end_turn"
    input_tokens: int = 100
    output_tokens: int = 200


class _FakeOllama:
    """Returns whatever text we hand it. Records the last call's prompt
    and system message so tests can assert on prompt construction."""
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.last_prompt = ""
        self.last_system = ""
        self.last_max_tokens = 0
        self.last_temperature = 0.0

    async def complete(self, prompt: str, *, system=None, max_tokens=1024,
                       temperature=0.2, **kw):
        self.last_prompt = prompt
        self.last_system = system or ""
        self.last_max_tokens = max_tokens
        self.last_temperature = temperature
        return _FakeResult(text=self.response_text)


# ---------------------------------------------------------------------------
# should_index — eligibility rules.
# ---------------------------------------------------------------------------

class TestShouldIndex(unittest.TestCase):
    def test_normal_file_indexed(self):
        ok, _ = should_index("app/main.py", 1200)
        self.assertTrue(ok)

    def test_large_file_skipped(self):
        ok, reason = should_index("app/main.py", MAX_INDEXABLE_BYTES + 1)
        self.assertFalse(ok)
        self.assertIn("too large", reason)

    def test_lockfile_skipped(self):
        ok, _ = should_index("yarn.lock", 500)
        self.assertFalse(ok)

    def test_node_modules_skipped(self):
        ok, _ = should_index("node_modules/react/index.js", 800)
        self.assertFalse(ok)

    def test_dist_skipped(self):
        ok, _ = should_index("dist/bundle.js", 5000)
        self.assertFalse(ok)

    def test_min_js_skipped(self):
        ok, _ = should_index("public/app.min.js", 5000)
        self.assertFalse(ok)

    def test_dot_next_skipped(self):
        ok, _ = should_index(".next/build/static/chunks/x.js", 500)
        self.assertFalse(ok)


# ---------------------------------------------------------------------------
# JSON parsing — the model output is the most fragile boundary.
# ---------------------------------------------------------------------------

class TestParseSummaryJson(unittest.TestCase):
    def test_clean_json(self):
        raw = '{"plain_english":"hi","technical":"hello","purpose":"x","touches":[],"assumes":[],"failure_modes":[],"risk_notes":[]}'
        parsed = _parse_summary_json(raw)
        self.assertEqual(parsed["plain_english"], "hi")
        self.assertEqual(parsed["technical"], "hello")

    def test_fenced_json(self):
        raw = '```json\n{"plain_english":"hi","technical":"x","purpose":"","touches":[],"assumes":[],"failure_modes":[],"risk_notes":[]}\n```'
        parsed = _parse_summary_json(raw)
        self.assertEqual(parsed["plain_english"], "hi")

    def test_malformed_returns_stub(self):
        """Defensive: malformed model output yields a stub with the raw
        text in `technical`, not an exception. The caller gets SOMETHING
        to persist rather than a crashed indexing job."""
        raw = "this is definitely not JSON {oops"
        parsed = _parse_summary_json(raw)
        self.assertIn("could not parse", parsed["plain_english"].lower())
        self.assertIn("oops", parsed["technical"])

    def test_array_at_top_level_returns_empty(self):
        """Models occasionally return [..] instead of {..}. Treat as
        unparseable to avoid KeyErrors downstream."""
        raw = '[{"key": "value"}]'
        parsed = _parse_summary_json(raw)
        # Should fall through to stub since it's not a dict.
        self.assertIsInstance(parsed, dict)


# ---------------------------------------------------------------------------
# Field coercion — models lie about types sometimes.
# ---------------------------------------------------------------------------

class TestCoerceStringList(unittest.TestCase):
    def test_list_of_strings_passes_through(self):
        self.assertEqual(
            _coerce_string_list(["a", "b", "c"]),
            ["a", "b", "c"],
        )

    def test_single_string_wrapped(self):
        self.assertEqual(_coerce_string_list("just one"), ["just one"])

    def test_list_of_dicts_extracts_text_field(self):
        self.assertEqual(
            _coerce_string_list([{"text": "hi"}, {"description": "there"}]),
            ["hi", "there"],
        )

    def test_none_returns_empty(self):
        self.assertEqual(_coerce_string_list(None), [])

    def test_max_items_enforced(self):
        many = [f"item-{i}" for i in range(50)]
        self.assertEqual(len(_coerce_string_list(many, max_items=5)), 5)

    def test_long_strings_truncated(self):
        result = _coerce_string_list(["x" * 1000])
        self.assertLessEqual(len(result[0]), 500)


# ---------------------------------------------------------------------------
# summarize_file — end-to-end with fake Ollama.
# ---------------------------------------------------------------------------

class TestSummarizeFile(unittest.TestCase):
    def test_happy_path_produces_full_summary(self):
        response = (
            '{"plain_english":"This file handles user logins.",'
            '"technical":"Defines the LoginHandler class with verify_password().",'
            '"purpose":"Authentication entry point",'
            '"touches":["users table","session cookies"],'
            '"assumes":["password is bcrypt-hashed"],'
            '"failure_modes":["DB connection lost during verify"],'
            '"risk_notes":["Should rate-limit by IP"]}'
        )
        client = _FakeOllama(response)
        summary = _run(summarize_file(
            file_path="app/auth.py",
            content="class LoginHandler:\n    def verify_password(self, p): ...",
            language="python",
            imports=["hashlib", "psycopg"],
            client=client,
        ))
        self.assertEqual(summary.file_path, "app/auth.py")
        self.assertEqual(summary.plain_english, "This file handles user logins.")
        self.assertIn("LoginHandler", summary.technical)
        self.assertEqual(summary.touches, ["users table", "session cookies"])
        self.assertEqual(len(summary.failure_modes), 1)
        self.assertEqual(summary.indexer_model, "qwen2.5-coder:14b")
        # Token counts surfaced for cost accounting.
        self.assertEqual(summary.input_tokens, 100)
        self.assertEqual(summary.output_tokens, 200)

    def test_prompt_includes_file_content_and_path(self):
        client = _FakeOllama(
            '{"plain_english":"x","technical":"y","purpose":"z","touches":[],"assumes":[],"failure_modes":[],"risk_notes":[]}'
        )
        _run(summarize_file(
            file_path="src/widgets/Button.tsx",
            content="export const Button = () => <button/>;",
            language="typescript",
            imports=["react"],
            client=client,
        ))
        self.assertIn("src/widgets/Button.tsx", client.last_prompt)
        self.assertIn("typescript", client.last_prompt)
        self.assertIn("export const Button", client.last_prompt)
        # System prompt should ask for strict JSON.
        self.assertIn("STRICT JSON", client.last_system)

    def test_imports_included_in_prompt(self):
        client = _FakeOllama(
            '{"plain_english":"","technical":"","purpose":"","touches":[],"assumes":[],"failure_modes":[],"risk_notes":[]}'
        )
        _run(summarize_file(
            file_path="x.py", content="...",
            language="python",
            imports=["fastapi", "sqlalchemy", "redis"],
            client=client,
        ))
        self.assertIn("fastapi", client.last_prompt)
        self.assertIn("sqlalchemy", client.last_prompt)

    def test_malformed_response_still_produces_summary(self):
        """If the model returns garbage, we still get a FileSummary back
        — just with empty fields and the raw text in `technical`. The
        indexing job doesn't fail."""
        client = _FakeOllama("totally not JSON at all")
        summary = _run(summarize_file(
            file_path="x.py", content="...",
            language="python", client=client,
        ))
        self.assertEqual(summary.file_path, "x.py")
        # Fields populated with stub values.
        self.assertEqual(summary.touches, [])
        self.assertIn("could not parse", summary.plain_english.lower())

    def test_low_temperature_used(self):
        """Summaries should use low temperature for consistency."""
        client = _FakeOllama(
            '{"plain_english":"","technical":"","purpose":"","touches":[],"assumes":[],"failure_modes":[],"risk_notes":[]}'
        )
        _run(summarize_file(
            file_path="x.py", content="...",
            language="python", client=client,
        ))
        self.assertLessEqual(client.last_temperature, 0.2)


# ---------------------------------------------------------------------------
# Ledger round-trip — write a summary, read it back.
# ---------------------------------------------------------------------------

class TestSummaryLedgerRoundtrip(unittest.TestCase):
    def test_write_then_load(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        summary = FileSummary(
            file_path="app/main.py",
            plain_english="Main entry point.",
            technical="Defines FastAPI app object.",
            purpose="HTTP server bootstrap",
            touches=["routes", "middleware"],
            assumes=["PORT env var set"],
            failure_modes=["DB connection refused at startup"],
            risk_notes=["Should add /healthz endpoint"],
            indexed_at=1234567890.0,
            indexer_model="qwen2.5-coder:14b",
            input_tokens=100, output_tokens=200,
        )
        write_file_summary(store, pid, summary)

        loaded = load_file_summaries(store, pid)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["file_path"], "app/main.py")
        self.assertEqual(loaded[0]["plain_english"], "Main entry point.")
        self.assertEqual(loaded[0]["touches"], ["routes", "middleware"])

    def test_re_summarize_supersedes(self):
        """Indexing the same file twice should leave only the latest
        summary in `current_artifacts`. This is the standard ledger
        versioning — we don't need to do anything special, just verify."""
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        for english in ["First version", "Second version"]:
            write_file_summary(store, pid, FileSummary(
                file_path="app/main.py",
                plain_english=english,
                technical="x", purpose="y",
                touches=[], assumes=[], failure_modes=[], risk_notes=[],
                indexed_at=0.0, indexer_model="m",
                input_tokens=0, output_tokens=0,
            ))
        loaded = load_file_summaries(store, pid)
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["plain_english"], "Second version")

    def test_multiple_files_multiple_summaries(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("test", "test")
        for path in ["app/a.py", "app/b.py", "app/c.py"]:
            write_file_summary(store, pid, FileSummary(
                file_path=path,
                plain_english=f"summary for {path}",
                technical="x", purpose="y",
                touches=[], assumes=[], failure_modes=[], risk_notes=[],
                indexed_at=0.0, indexer_model="m",
                input_tokens=0, output_tokens=0,
            ))
        loaded = load_file_summaries(store, pid)
        self.assertEqual(len(loaded), 3)
        paths = {s["file_path"] for s in loaded}
        self.assertEqual(paths, {"app/a.py", "app/b.py", "app/c.py"})


if __name__ == "__main__":
    unittest.main()
