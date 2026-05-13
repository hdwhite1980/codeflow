"""Tests for OllamaClient.

Uses httpx's MockTransport to fake the Ollama HTTP API, so we exercise
the full request/response/fallback path without a real daemon.
"""

from __future__ import annotations

import asyncio
import json
import unittest

import httpx

from ollama_client import (
    OllamaClient,
    OllamaError,
    OllamaUnavailable,
)


def _run(coro):
    return asyncio.run(coro)


def _make_client_with_handler(handler):
    """Construct an OllamaClient whose httpx is backed by a mock transport.

    `handler` is a callable that takes a httpx.Request and returns a
    httpx.Response. We swap out the client's internal _http to use it.
    """
    client = OllamaClient(base_url="http://test")
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://test",
        timeout=5.0,
        headers={"content-type": "application/json"},
    )
    return client


class TestOllamaClient(unittest.TestCase):
    def test_happy_path(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(request.url.path, "/api/generate")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "qwen2.5-coder:14b")
            self.assertEqual(body["prompt"], "say hi")
            self.assertFalse(body["stream"])
            return httpx.Response(200, json={
                "response": "hi there",
                "model": "qwen2.5-coder:14b",
                "done_reason": "stop",
                "prompt_eval_count": 5,
                "eval_count": 10,
            })

        client = _make_client_with_handler(handler)
        result = _run(client.complete("say hi"))
        self.assertEqual(result.text, "hi there")
        self.assertEqual(result.input_tokens, 5)
        self.assertEqual(result.output_tokens, 10)
        self.assertEqual(result.stop_reason, "end_turn")  # normalized

    def test_system_prompt_passed(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={
                "response": "ok", "model": "qwen2.5-coder:14b",
                "done_reason": "stop",
                "prompt_eval_count": 1, "eval_count": 1,
            })

        client = _make_client_with_handler(handler)
        _run(client.complete("hi", system="you are a test"))
        self.assertEqual(captured["body"]["system"], "you are a test")

    def test_404_falls_back_to_smaller_model(self):
        """If the configured 14b model isn't installed, the client should
        try 7b next without raising."""
        call_log = []

        def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            call_log.append(body["model"])
            if body["model"] == "qwen2.5-coder:14b":
                # Simulate "model not found" — Ollama returns 404.
                return httpx.Response(
                    404, json={"error": "model 'qwen2.5-coder:14b' not found"},
                )
            return httpx.Response(200, json={
                "response": "served by fallback",
                "model": body["model"],
                "done_reason": "stop",
                "prompt_eval_count": 5, "eval_count": 5,
            })

        client = _make_client_with_handler(handler)
        result = _run(client.complete("test"))
        self.assertEqual(result.text, "served by fallback")
        # First attempt was the primary 14b, second was 7b fallback.
        self.assertEqual(call_log[:2], ["qwen2.5-coder:14b", "qwen2.5-coder:7b"])
        # Subsequent calls remember the working model.
        _run(client.complete("again"))
        self.assertEqual(call_log[-1], "qwen2.5-coder:7b")

    def test_all_models_404_raises(self):
        """If NO model in the fallback chain is available, raise so the
        caller knows to skip the indexing pass."""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "no models"})

        client = _make_client_with_handler(handler)
        with self.assertRaises(OllamaError):
            _run(client.complete("test"))

    def test_500_raises_immediately_no_fallback(self):
        """A 500 isn't model-specific — fallback won't help. Don't try."""
        attempts = []

        def handler(request: httpx.Request) -> httpx.Response:
            attempts.append(1)
            return httpx.Response(500, text="internal error")

        client = _make_client_with_handler(handler)
        with self.assertRaises(OllamaError) as ctx:
            _run(client.complete("test"))
        self.assertEqual(ctx.exception.status_code, 500)
        # Only the primary model was tried; no fallback for 5xx.
        self.assertEqual(len(attempts), 1)

    def test_network_error_raises_unavailable(self):
        """If httpx itself fails (e.g. connection refused), we raise the
        distinct OllamaUnavailable so callers can specifically defer."""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        client = _make_client_with_handler(handler)
        with self.assertRaises(OllamaUnavailable):
            _run(client.complete("test"))


if __name__ == "__main__":
    unittest.main()
