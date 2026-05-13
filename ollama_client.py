"""
codeflow.ollama_client
======================

Async client for a local Ollama server. Mirrors the interface of
AnthropicClient / OpenAIClient / GeminiClient so the rest of the
pipeline can treat any of them as "a thing with .complete()".

Why local
---------
The guardian (semantic indexer + risk analyzer + ambient reviewer)
runs continuously and asks short questions. Frontier APIs would cost
too much and leak too much code to third parties. Ollama on the
customer's own hardware (or the Hetzner box for our managed service)
keeps the code in their perimeter and the per-call cost at zero.

Server lifecycle
----------------
We don't manage the Ollama daemon — it runs as a systemd service on
the Hetzner box. This client just talks to its HTTP API. If the
daemon is down, calls fail with a clear error and the caller decides
how to degrade (typically: skip the index and try again on the next
cycle).

Default model
-------------
We default to `qwen2.5-coder:14b`. The guardian's task is
summarization and reasoning, not generation, so model size beats
speed for quality. 14b is the sweet spot on a single mid-range GPU.

Tokenization
------------
Ollama's /api/generate response includes `prompt_eval_count` (input
tokens) and `eval_count` (output tokens) when streaming is off. We
surface these in CompletionResult for cost-comparison parity with
the frontier clients — even though Ollama is free, knowing how much
compute we used is useful for capacity planning.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx


# Default model. Qwen 2.5 Coder 14B handles code summarization well and
# fits in ~10GB VRAM at 4-bit quantization. Override via OLLAMA_MODEL.
DEFAULT_MODEL = "qwen2.5-coder:14b"

# Fallback chain if the primary model isn't available on the box.
# We try the configured model first; if Ollama responds with model-not-found,
# we degrade in order. The guardian keeps working at lower quality rather
# than failing entirely.
FALLBACK_MODELS = ["qwen2.5-coder:7b", "qwen2.5-coder:3b"]

# Per-call timeout. Summarization prompts return in 5-15 seconds on 14b;
# risk-analysis prompts can take 30+ if the model is unloaded from VRAM
# and has to reload. We allow generous headroom.
DEFAULT_TIMEOUT_SECONDS = 90.0

# Base URL of the Ollama daemon. Default points at the Hetzner box's
# internal address; override via OLLAMA_BASE_URL for local dev.
DEFAULT_BASE_URL = os.environ.get(
    "OLLAMA_BASE_URL", "http://5.78.79.75:11434"
)


# ---------------------------------------------------------------------------
# Error types.
# ---------------------------------------------------------------------------

class OllamaError(Exception):
    """An Ollama call failed. Carries status code and response body so the
    caller can decide whether to retry, fall back to another model, or
    give up on the indexing pass."""

    def __init__(
        self, message: str, *, status_code: Optional[int] = None,
        body: str = "",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class OllamaUnavailable(OllamaError):
    """Daemon is unreachable. Distinct from generic errors so the caller
    can specifically choose to defer (rather than retry within the same
    request)."""


# ---------------------------------------------------------------------------
# Response shape.
# ---------------------------------------------------------------------------

@dataclass
class CompletionResult:
    """Mirrors AnthropicClient.CompletionResult / OpenAIClient.CompletionResult.

    text          : the generated completion
    model         : which model actually produced this (may differ from
                    requested if we fell back)
    input_tokens  : prompt_eval_count from Ollama (input tokens)
    output_tokens : eval_count from Ollama (generated tokens)
    stop_reason   : "stop" | "length" | "error" — Ollama returns "done" with
                    a "done_reason" field. We normalize to match the other
                    clients' vocabulary.
    """
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

class OllamaClient:
    """Async client for the local Ollama HTTP API.

    Lifecycle
    ---------
    Construct once at startup, reuse across calls. The httpx.AsyncClient
    pools connections. Caller is responsible for `await client.aclose()`
    on shutdown.

    Model fallback
    --------------
    The configured model may not be present on the box (the operator
    forgot to `ollama pull`, or the box was reprovisioned). Rather than
    failing every call, we degrade to a smaller model from FALLBACK_MODELS
    if the requested one returns 404. This is logged so the operator
    knows to install the bigger model when they have time.
    """

    def __init__(
        self,
        *,
        default_model: str = DEFAULT_MODEL,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._default_model = default_model
        self._base_url = base_url.rstrip("/")
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout_seconds,
            headers={"content-type": "application/json"},
        )
        # Track which model we actually got working. Set after the first
        # successful call so subsequent calls don't repeat the fallback
        # discovery work.
        self._working_model: Optional[str] = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health_check(self) -> bool:
        """Return True if the Ollama daemon is reachable.

        Used by the worker on startup to decide whether the guardian
        subsystem can be enabled at all. If this returns False, the
        worker logs a warning and disables guardian job kinds for the
        session — the rest of the pipeline (build/audit/iterate/fix-all)
        runs normally."""
        try:
            resp = await self._http.get("/api/tags", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False

    async def complete(
        self,
        prompt: str,
        *,
        system: Optional[str] = None,
        model: Optional[str] = None,
        max_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> CompletionResult:
        """Send one prompt, return the response.

        Signature matches AnthropicClient.complete so callers can swap
        clients via dependency injection if needed.
        """
        # Choose model: explicit override > previously-working > default.
        primary = model or self._working_model or self._default_model
        # Order: primary first, then fallbacks (de-duped, preserving order).
        attempt_models: list[str] = [primary] + [
            m for m in FALLBACK_MODELS if m != primary
        ]

        last_error: Optional[OllamaError] = None
        for candidate in attempt_models:
            try:
                result = await self._call_generate(
                    model=candidate,
                    prompt=prompt,
                    system=system,
                    max_tokens=max_tokens,
                    temperature=temperature,
                )
                # Remember this model worked, so we skip fallback discovery
                # on the next call. If the operator later installs the
                # primary, the next process restart will re-attempt it.
                self._working_model = candidate
                return result
            except OllamaError as exc:
                last_error = exc
                # 404 = model not present on the box. Try the next fallback.
                if exc.status_code == 404:
                    print(f"[ollama] model {candidate!r} not available; "
                          f"trying next fallback", flush=True)
                    continue
                # Other errors are not model-specific. Stop trying.
                raise

        # All fallbacks exhausted.
        raise OllamaError(
            f"No working Ollama model. Tried: {', '.join(attempt_models)}. "
            f"Last error: {last_error}",
            status_code=(last_error.status_code if last_error else None),
        )

    async def _call_generate(
        self,
        *,
        model: str,
        prompt: str,
        system: Optional[str],
        max_tokens: int,
        temperature: float,
    ) -> CompletionResult:
        """One HTTP call to /api/generate. Returns CompletionResult or
        raises OllamaError. Streaming is disabled — we want the whole
        response at once with token-count metadata included."""
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }
        if system:
            payload["system"] = system

        try:
            resp = await self._http.post("/api/generate", json=payload)
        except httpx.HTTPError as exc:
            raise OllamaUnavailable(
                f"Ollama daemon unreachable at {self._base_url}: "
                f"{type(exc).__name__}: {exc}",
            ) from exc

        if resp.status_code == 404:
            # Ollama returns 404 with a body like {"error":"model 'X' not found"}.
            raise OllamaError(
                f"Model not found: {model}",
                status_code=404,
                body=resp.text[:500],
            )

        if resp.status_code >= 400:
            raise OllamaError(
                f"Ollama returned HTTP {resp.status_code}",
                status_code=resp.status_code,
                body=resp.text[:500],
            )

        try:
            data = resp.json()
        except json.JSONDecodeError as exc:
            raise OllamaError(
                f"Could not parse Ollama response as JSON: {exc}",
                body=resp.text[:500],
            ) from exc

        text = data.get("response", "")
        # Normalize done_reason to match the other clients' stop_reason
        # vocabulary. Ollama's "stop" maps to our "end_turn"; "length"
        # means we hit num_predict.
        done_reason = data.get("done_reason", "stop")
        stop_reason = "end_turn" if done_reason == "stop" else done_reason

        return CompletionResult(
            text=text,
            model=model,
            stop_reason=stop_reason,
            input_tokens=int(data.get("prompt_eval_count", 0) or 0),
            output_tokens=int(data.get("eval_count", 0) or 0),
        )
