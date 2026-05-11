"""
codeflow.anthropic_client
=========================

Minimal async client for the Anthropic Messages API. We don't use the
official `anthropic` SDK on purpose — it's a heavy dependency that pulls
in pydantic versions of its own and a model abstraction we don't need.
What we want is "POST to /v1/messages, parse JSON, return text". httpx
handles that in ~100 lines, fully typed, with no surprises.

Design choices
--------------
*Async only.* The worker dispatches handlers as coroutines and the web
service runs on Starlette. There's no synchronous code path that needs an
LLM call. Keeping the client async-only halves the code and avoids the
classic "wrap a sync call in run_in_executor" trap.

*One method.* `complete()` is the entire surface. No streaming, no
structured outputs, no tool use. The build pipeline asks for JSON in the
prompt and parses the response itself. We can add streaming later when
the frontend wants tokens-as-they-arrive, but right now nothing consumes
a stream.

*Pinned model in code, overridable per call.* The default model is set
at instantiation. Callers can pass `model=` to override. We do not read
the model from env vars because changing models silently in production is
a recipe for inexplicable regression — model bumps should be a code
change with a code review.

*Structured exceptions.* AnthropicError carries the status code and
response body so handlers can decide how to react. Rate limits look
different from prompt-too-long looks different from auth failures.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx


# Default model. Sonnet 4.6 is the right balance for our build pipeline:
# strong enough for spec generation and file authoring, far cheaper than
# Opus at the volume we'll hit when a single project spawns 20+ calls.
# Bump deliberately, via PR, not via env override.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Hard cap on output tokens per call. The Messages API requires this
# parameter and using too large a value can cause unintentional
# overspending. 4096 is plenty for a single file or a spec; the build
# pipeline issues one call per file rather than asking for everything at
# once, which is also what keeps individual prompts focused.
DEFAULT_MAX_TOKENS = 4096

# Per-call HTTP timeout. AI calls can take a while; this is generous but
# not infinite. If a call exceeds this, something is wrong (or the model
# is genuinely stuck) and we'd rather fail loud than hold the worker.
DEFAULT_TIMEOUT_SECONDS = 120.0

# The API version header. This is a contract between us and Anthropic:
# bump it deliberately when adopting new features. Older versions stay
# supported, so there's no rush.
API_VERSION = "2023-06-01"

API_BASE = "https://api.anthropic.com"


# ---------------------------------------------------------------------------
# Error types.
# ---------------------------------------------------------------------------

class AnthropicError(Exception):
    """Base for all errors from the client.

    Carries the HTTP status code and the raw response body so callers can
    decide whether to retry, give up, or surface to the user. Subclasses
    are conveniences for the most common cases."""

    def __init__(self, status_code: int, body: str, message: str = "") -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"Anthropic API error {status_code}: {body[:200]}")


class AnthropicAuthError(AnthropicError):
    """401 or 403. The API key is missing, wrong, or revoked. Not retryable."""


class AnthropicRateLimitError(AnthropicError):
    """429. Slow down. Backoff and retry might help, but we let the caller
    decide — the worker has its own pacing concerns."""


class AnthropicBadRequest(AnthropicError):
    """400. Usually a prompt-too-long or malformed parameter. Not retryable
    without changing the request."""


class AnthropicServerError(AnthropicError):
    """5xx. Anthropic-side issue. Retry is usually fine but the caller
    chooses; we don't bake retry into the client because the right backoff
    depends on the context (a build job vs an interactive request)."""


def _classify(status_code: int, body: str) -> AnthropicError:
    """Pick the right subclass for the status code."""
    if status_code in (401, 403):
        return AnthropicAuthError(status_code, body)
    if status_code == 429:
        return AnthropicRateLimitError(status_code, body)
    if 400 <= status_code < 500:
        return AnthropicBadRequest(status_code, body)
    return AnthropicServerError(status_code, body)


# ---------------------------------------------------------------------------
# Response shape.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompletionResult:
    """What we keep from an Anthropic response.

    We deliberately don't expose the raw envelope — content blocks, stop
    reasons, internal IDs. Callers want the text, the token usage (for
    cost tracking), and the stop reason (to detect "ran out of tokens").
    If anyone needs more, add it here, don't reach into raw."""

    text: str
    model: str
    stop_reason: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ---------------------------------------------------------------------------
# Client.
# ---------------------------------------------------------------------------

class AnthropicClient:
    """Async client for the Messages API.

    Construct once at startup, reuse across calls. The httpx.AsyncClient
    underneath maintains a connection pool — important for the build
    pipeline, which fires many calls in rapid succession.

    Lifecycle
    ---------
    Owns an httpx.AsyncClient. Caller is responsible for closing the
    client when done (via `await client.aclose()`). The worker process
    constructs one of these at startup and aclose()s on SIGTERM.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        default_model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        base_url: str = API_BASE,
    ) -> None:
        # Falling back to env keeps the constructor zero-arg in production
        # but lets tests pass a fake key without touching the environment.
        key = api_key if api_key is not None else os.environ.get("ANTHROPIC_API_KEY", "")
        if not key:
            raise RuntimeError(
                "ANTHROPIC_API_KEY is required. Set it in the worker's "
                "Railway service variables."
            )
        self._api_key = key
        self._default_model = default_model
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "x-api-key": key,
                "anthropic-version": API_VERSION,
                "content-type": "application/json",
            },
        )

    async def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        temperature: float = 0.2,
    ) -> CompletionResult:
        """Send one prompt, return the text response.

        Parameters
        ----------
        prompt : the user message.
        system : optional system prompt. We use this for the "you are a
            spec generator" / "you are a file author" personas in the
            build pipeline.
        model  : override the default model. Use sparingly — model swaps
            change behavior, and the rest of the pipeline assumes
            consistency.
        max_tokens : output cap. Defaults to 4096 because that's enough
            for any single file or spec we generate today.
        temperature : 0.2 is low but not zero. We want some determinism
            for the build pipeline but not pure greedy (which produces
            stiff, brittle output in long completions).

        Returns
        -------
        CompletionResult with text, model id, stop reason, and token
        counts.
        """
        body: dict[str, Any] = {
            "model": model or self._default_model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if system is not None:
            body["system"] = system

        response = await self._http.post("/v1/messages", json=body)
        if response.status_code != 200:
            raise _classify(response.status_code, response.text)

        data = response.json()

        # The response envelope has a `content` array of blocks. For a
        # plain prompt we expect exactly one text block; if the API ever
        # returns multiple blocks (e.g. tool use, which we don't enable),
        # we concatenate the text-typed ones and ignore the rest. That
        # keeps callers from breaking on shape changes that don't matter
        # to them.
        text_parts: list[str] = []
        for block in data.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
        text = "".join(text_parts)

        usage = data.get("usage", {})
        return CompletionResult(
            text=text,
            model=data.get("model", body["model"]),
            stop_reason=data.get("stop_reason", "unknown"),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
