"""
codeflow.gemini_client
======================

Minimal async client for the Gemini generateContent API. Same shape as
anthropic_client and openai_client: thin httpx wrapper, one method, no
SDK dependency, structured errors.

Endpoint differences vs the other two providers
------------------------------------------------
- Auth header is `x-goog-api-key`, not Bearer.
- Base URL is generativelanguage.googleapis.com/v1beta.
- Model lives in the URL path (`/models/<model>:generateContent`),
  not in the request body.
- "messages" are called "contents" and use {"role": "user|model",
  "parts": [{"text": "..."}]}.
- System prompts go in a top-level "systemInstruction", not in the
  contents array.
- Token usage is in `usageMetadata.promptTokenCount` and
  `usageMetadata.candidatesTokenCount`.
- JSON mode is `generationConfig.responseMimeType = "application/json"`.
- Stop reasons (called "finishReason"): "STOP", "MAX_TOKENS", "SAFETY",
  "RECITATION", "OTHER".

Default model
-------------
gemini-3-flash-preview. Flash tier is the cost-sensitive workhorse —
similar role to gpt-5.4-mini for OpenAI. For code audit (read-heavy,
structured-output) we don't need the Pro tier's deeper reasoning.

If a project loses access to flash-preview (preview naming churn or
account tier), fall back to `gemini-2.5-flash`, which is the previous
generation and broadly available.
"""

from __future__ import annotations

import asyncio
import os
import random
from dataclasses import dataclass
from typing import Any, Optional

import httpx


# Default model. flash-preview is the equivalent tier to claude-sonnet-4-6
# and gpt-5.4-mini for our purposes — fast, cheap, good at structured
# output. Bump deliberately when we want a quality lift.
DEFAULT_MODEL = "gemini-3-flash-preview"

# Output cap. Like the OpenAI client, set generous because Gemini's
# reasoning models can spend a chunk of tokens internally before
# producing visible output.
DEFAULT_MAX_TOKENS = 8192

DEFAULT_TIMEOUT_SECONDS = 90.0

# Transient-error retry configuration. We retry on 429 (rate limit) and
# 5xx (server unavailable / internal error). Both are transient by
# definition; everything else (4xx auth/bad-request) is permanent for
# this request and bubbles out unchanged.
#
# Sizing: with 4 retries the delays land roughly at 4s, 8s, 16s, 32s —
# total ~60s of patience. That comfortably crosses a per-minute rate
# limit window and gives a typical 503 outage time to recover.
MAX_TRANSIENT_RETRIES = 4
TRANSIENT_BASE_DELAY_SECONDS = 4.0
TRANSIENT_MAX_DELAY_SECONDS = 32.0
# Status codes that are worth retrying. 429 = rate limit (per-minute or
# per-day quota); 5xx = transient server-side issue. Anything else is
# a permanent failure for this particular request.
_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})

API_BASE = "https://generativelanguage.googleapis.com"
API_VERSION = "v1beta"


# ---------------------------------------------------------------------------
# Error types — mirror the structure of AnthropicError/OpenAIError.
# ---------------------------------------------------------------------------

class GeminiError(Exception):
    def __init__(self, status_code: int, body: str, message: str = "") -> None:
        self.status_code = status_code
        self.body = body
        super().__init__(message or f"Gemini API error {status_code}: {body[:200]}")


class GeminiAuthError(GeminiError):
    """401 or 403. Bad/missing/revoked key, or the project lacks access to
    the requested model. Not retryable."""


class GeminiRateLimitError(GeminiError):
    """429. Backoff and retry might help."""


class GeminiBadRequest(GeminiError):
    """400. Usually parameter or prompt issue. Not retryable as-is."""


class GeminiServerError(GeminiError):
    """5xx. Retry can help."""


def _classify(status_code: int, body: str) -> GeminiError:
    if status_code in (401, 403):
        return GeminiAuthError(status_code, body)
    if status_code == 429:
        return GeminiRateLimitError(status_code, body)
    if 400 <= status_code < 500:
        return GeminiBadRequest(status_code, body)
    return GeminiServerError(status_code, body)


# ---------------------------------------------------------------------------
# Response shape — mirrors CompletionResult in the other clients.
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

class GeminiClient:
    """Async client for the Gemini generateContent API.

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
        key = api_key if api_key is not None else os.environ.get("GEMINI_API_KEY", "")
        if not key:
            raise RuntimeError(
                "GEMINI_API_KEY is required. Set it in the worker's "
                "Railway service variables."
            )
        self._api_key = key
        self._default_model = default_model
        self._http = httpx.AsyncClient(
            base_url=base_url,
            timeout=timeout_seconds,
            headers={
                "x-goog-api-key": key,
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
        system : optional system instruction.
        model  : override the default model.
        max_tokens : output cap (maxOutputTokens in the API).
        temperature : 0.2 default — audit wants determinism, not creativity.
        json_mode : if True, request responseMimeType=application/json.
        """
        model_id = model or self._default_model

        generation_config: dict[str, Any] = {
            "maxOutputTokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            generation_config["responseMimeType"] = "application/json"

        body: dict[str, Any] = {
            "contents": [
                {"role": "user", "parts": [{"text": prompt}]},
            ],
            "generationConfig": generation_config,
        }
        if system is not None:
            # System prompts go in their own top-level field, separate
            # from the contents array.
            body["systemInstruction"] = {
                "parts": [{"text": system}],
            }

        # Model is part of the URL, not the body.
        url = f"/{API_VERSION}/models/{model_id}:generateContent"

        # Retry loop for transient errors. 429 (rate limit) was the
        # original motivator — Tier 1 makes 429s rare but doesn't
        # eliminate them, and 5xx outages happen too (we saw a 503 in
        # production). Backoff is the same for both.
        for attempt in range(MAX_TRANSIENT_RETRIES + 1):
            response = await self._http.post(url, json=body)
            if response.status_code not in _RETRYABLE_STATUSES:
                break
            if attempt >= MAX_TRANSIENT_RETRIES:
                # Exhausted retries. Treat as a real failure of whatever
                # class the status code implies.
                raise _classify(response.status_code, response.text)
            delay = min(
                TRANSIENT_BASE_DELAY_SECONDS * (2 ** attempt),
                TRANSIENT_MAX_DELAY_SECONDS,
            )
            # Add jitter so concurrent retries don't all wake at the
            # same instant and stampede again. Up to 25% of the delay.
            jitter = delay * 0.25 * random.random()
            wait_s = delay + jitter
            print(f"[gemini] {response.status_code} on {model_id} "
                  f"attempt {attempt + 1}; sleeping {wait_s:.1f}s before retry",
                  flush=True)
            await asyncio.sleep(wait_s)

        if response.status_code != 200:
            raise _classify(response.status_code, response.text)

        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            raise GeminiServerError(
                200, response.text,
                "Gemini returned 200 but no candidates in response",
            )

        candidate = candidates[0]
        # Some safety blocks can leave content empty. We tolerate that
        # (return empty text) and let the caller decide what to do.
        content = candidate.get("content", {})
        parts = content.get("parts", []) or []
        text_parts = [p.get("text", "") for p in parts if isinstance(p, dict)]
        text = "".join(text_parts)

        finish_reason = candidate.get("finishReason", "UNKNOWN")

        usage = data.get("usageMetadata", {})
        return CompletionResult(
            text=text,
            model=data.get("modelVersion", model_id),
            finish_reason=finish_reason,
            input_tokens=int(usage.get("promptTokenCount", 0)),
            output_tokens=int(usage.get("candidatesTokenCount", 0)),
        )

    async def aclose(self) -> None:
        await self._http.aclose()
