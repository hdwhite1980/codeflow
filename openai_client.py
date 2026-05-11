"""
codeflow.openai_client
======================

Minimal async client for the OpenAI Chat Completions API. Built the same
way as anthropic_client: a thin httpx wrapper, one method (`complete`),
structured error types. We do not depend on the official `openai` SDK
because we already pay that cost with httpx and don't need the SDK's
extra abstractions.

Why this exists separately from anthropic_client
------------------------------------------------
Different endpoints, different envelope shapes, different parameter
names. A unified "LLMClient" abstraction would be premature here — every
provider's API has small differences (system prompts vs messages,
temperature ranges, structured output formats) that leak through any
abstraction we'd build. Two parallel clients with the same shape but
different innards is the honest factoring.

The build pipeline imports `AnthropicClient`. The audit pipeline imports
`OpenAIClient`. Neither needs to know the other exists.

Defaults
--------
Default model is `gpt-5.4-mini`. For our use case (auditing generated
code) it's the sweet spot: stronger than nano on reasoning quality
where it matters, far cheaper than the flagship at high call volumes.
Bump deliberately if eval data shows the audit quality is insufficient.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx


# Default model. We pick mini for audit because audit is mostly reading
# (input-heavy) with short structured output, and mini-class models are
# accurate enough on "is this code OK" tasks. The price difference vs
# flagship is meaningful at our expected call volume.
DEFAULT_MODEL = "gpt-5.4-mini"

# Output cap. Audit findings are structured JSON — usually 200-1000
# tokens total. 2048 leaves room for a verbose response without
# allowing runaway generations to inflate cost.
DEFAULT_MAX_TOKENS = 2048

# Per-call HTTP timeout. Audit calls should be faster than generation
# calls because output is shorter; 90s is generous but bounded.
DEFAULT_TIMEOUT_SECONDS = 90.0

API_BASE = "https://api.openai.com"


# ---------------------------------------------------------------------------
# Error types — mirror the Anthropic client so callers can write
# provider-agnostic handler logic where needed.
# ---------------------------------------------------------------------------

class OpenAIError(Exception):
    def __init__(self, status_code: int, body: str, message: str = "") -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"OpenAI API error {status_code}: {body[:200]}")


class OpenAIAuthError(OpenAIError):
    """401 or 403. Bad/missing/revoked key. Not retryable."""


class OpenAIRateLimitError(OpenAIError):
    """429. Backoff and retry might help."""


class OpenAIBadRequest(OpenAIError):
    """400. Usually parameter or prompt issue. Not retryable as-is."""


class OpenAIServerError(OpenAIError):
    """5xx. Retry can help; caller decides backoff."""


def _classify(status_code: int, body: str) -> OpenAIError:
    if status_code in (401, 403):
        return OpenAIAuthError(status_code, body)
    if status_code == 429:
        return OpenAIRateLimitError(status_code, body)
    if 400 <= status_code < 500:
        return OpenAIBadRequest(status_code, body)
    return OpenAIServerError(status_code, body)


# ---------------------------------------------------------------------------
# Response shape — mirrors CompletionResult in anthropic_client.py.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class CompletionResult:
    text: str
    model: str
    finish_reason: str
    input_tokens: int
    output_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ---------------------------------------------------------------------------
# Client.
# ---------------------------------------------------------------------------

class OpenAIClient:
    """Async client for the OpenAI Chat Completions API.

    Construct once at startup, reuse across calls. Caller is responsible
    for aclose() at shutdown."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        default_model: str = DEFAULT_MODEL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        base_url: str = API_BASE,
    ) -> None:
        key = api_key if api_key is not None else os.environ.get("OPENAI_API_KEY", "")
        if not key:
            raise RuntimeError(
                "OPENAI_API_KEY is required. Set it in the worker's "
                "Railway service variables."
            )
        self._api_key = key
        self._default_model = default_model
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
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
        json_mode: bool = False,
    ) -> CompletionResult:
        """Send one prompt, return the text response.

        Parameters
        ----------
        prompt : the user message.
        system : optional system prompt.
        model  : override the default model.
        max_tokens : output cap.
        temperature : 0.2 default — auditing wants determinism, not creativity.
        json_mode : if True, request response_format={"type": "json_object"}.
            The audit pipeline uses this to guarantee parseable findings.

        Returns
        -------
        CompletionResult with text, model id, finish reason, and token counts.
        """
        messages: list[dict[str, Any]] = []
        if system is not None:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        body: dict[str, Any] = {
            "model": model or self._default_model,
            "messages": messages,
            "max_completion_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}

        response = await self._http.post("/v1/chat/completions", json=body)
        if response.status_code != 200:
            raise _classify(response.status_code, response.text)

        data = response.json()
        choices = data.get("choices", [])
        if not choices:
            # The API returned 200 but no choices. Shouldn't happen, but
            # raise a structured error so the caller can react.
            raise OpenAIServerError(
                200, response.text,
                "OpenAI returned 200 but no choices in response",
            )

        choice = choices[0]
        message = choice.get("message", {})
        text = message.get("content", "") or ""

        usage = data.get("usage", {})
        return CompletionResult(
            text=text,
            model=data.get("model", body["model"]),
            finish_reason=choice.get("finish_reason", "unknown"),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
