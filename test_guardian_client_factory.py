"""Tests for guardian_client_factory.py.

We exercise the selection logic — backend name resolution, client
construction for each backend, missing-env-var fallback to None.
We don't actually call the underlying clients; that's covered by
each client's own tests.
"""

from __future__ import annotations

import importlib
import os
import unittest

import guardian_client_factory


class _EnvIsolation(unittest.TestCase):
    """Helper: save/restore env vars touched by the factory across tests
    so tests don't bleed state into each other."""

    _KEYS = [
        "GUARDIAN_INDEXING_BACKEND",
        "ANTHROPIC_API_KEY",
        "OLLAMA_BASE_URL", "OLLAMA_API_TOKEN", "OLLAMA_VERIFY_TLS",
        "OLLAMA_TIMEOUT", "OLLAMA_DEFAULT_MODEL",
    ]

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in self._KEYS}
        for k in self._KEYS:
            os.environ.pop(k, None)
        # Reload so module-level constants are re-evaluated cleanly.
        importlib.reload(guardian_client_factory)

    def tearDown(self) -> None:
        for k in self._KEYS:
            os.environ.pop(k, None)
            if self._saved[k] is not None:
                os.environ[k] = self._saved[k]
        importlib.reload(guardian_client_factory)


class TestChosenBackend(_EnvIsolation):
    def test_default_is_claude_haiku(self):
        self.assertEqual(
            guardian_client_factory.chosen_backend(),
            "claude-haiku",
        )

    def test_explicit_claude_haiku(self):
        os.environ["GUARDIAN_INDEXING_BACKEND"] = "claude-haiku"
        self.assertEqual(
            guardian_client_factory.chosen_backend(),
            "claude-haiku",
        )

    def test_explicit_ollama(self):
        os.environ["GUARDIAN_INDEXING_BACKEND"] = "ollama"
        self.assertEqual(
            guardian_client_factory.chosen_backend(),
            "ollama",
        )

    def test_case_insensitive(self):
        os.environ["GUARDIAN_INDEXING_BACKEND"] = "OLLAMA"
        self.assertEqual(
            guardian_client_factory.chosen_backend(),
            "ollama",
        )

    def test_unknown_falls_back_to_claude(self):
        os.environ["GUARDIAN_INDEXING_BACKEND"] = "not-a-real-backend"
        self.assertEqual(
            guardian_client_factory.chosen_backend(),
            "claude-haiku",
        )

    def test_empty_string_falls_back_to_default(self):
        os.environ["GUARDIAN_INDEXING_BACKEND"] = ""
        self.assertEqual(
            guardian_client_factory.chosen_backend(),
            "claude-haiku",
        )


class TestMakeGuardianClientClaudePath(_EnvIsolation):
    def test_constructs_when_anthropic_key_present(self):
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        c = guardian_client_factory.make_guardian_client()
        from anthropic_client import AnthropicClient
        self.assertIsInstance(c, AnthropicClient)

    def test_returns_none_when_anthropic_key_missing(self):
        # Default backend is claude-haiku; no key → returns None.
        # We don't crash, callers handle None as "indexing disabled."
        c = guardian_client_factory.make_guardian_client()
        self.assertIsNone(c)

    def test_uses_haiku_4_5_model(self):
        """Pinned: guardian indexing uses Haiku for speed. If anything
        else accidentally takes over the indexing backend we want to
        catch it here."""
        os.environ["ANTHROPIC_API_KEY"] = "fake-key"
        c = guardian_client_factory.make_guardian_client()
        # AnthropicClient stores its default_model as _default_model
        # (or similar). Check the value matches.
        self.assertEqual(
            getattr(c, "_default_model", None),
            "claude-haiku-4-5",
        )


class TestMakeGuardianClientOllamaPath(_EnvIsolation):
    def test_constructs_when_base_url_present(self):
        os.environ["GUARDIAN_INDEXING_BACKEND"] = "ollama"
        os.environ["OLLAMA_BASE_URL"] = "https://example.com/ollama"
        c = guardian_client_factory.make_guardian_client()
        from ollama_client import OllamaClient
        self.assertIsInstance(c, OllamaClient)

    def test_returns_none_when_base_url_missing(self):
        os.environ["GUARDIAN_INDEXING_BACKEND"] = "ollama"
        c = guardian_client_factory.make_guardian_client()
        self.assertIsNone(c)

    def test_respects_ollama_default_model(self):
        os.environ["GUARDIAN_INDEXING_BACKEND"] = "ollama"
        os.environ["OLLAMA_BASE_URL"] = "https://example.com/ollama"
        os.environ["OLLAMA_DEFAULT_MODEL"] = "qwen2.5-coder:14b"
        c = guardian_client_factory.make_guardian_client()
        self.assertEqual(
            getattr(c, "_default_model", None),
            "qwen2.5-coder:14b",
        )

    def test_falls_back_to_qwen_7b_when_no_model_env(self):
        os.environ["GUARDIAN_INDEXING_BACKEND"] = "ollama"
        os.environ["OLLAMA_BASE_URL"] = "https://example.com/ollama"
        c = guardian_client_factory.make_guardian_client()
        self.assertEqual(
            getattr(c, "_default_model", None),
            "qwen2.5-coder:7b",
        )


if __name__ == "__main__":
    unittest.main()
