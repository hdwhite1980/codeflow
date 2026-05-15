"""
codeflow.guardian_client_factory
================================

Picks the LLM client used for guardian indexing (file-level + symbol-
level semantic summaries).

Two backends are supported:

  - **claude-haiku** (default): Claude Haiku 4.5 via the existing
    AnthropicClient. Roughly 20–30× faster than local Ollama on CPU
    and produces noticeably better summaries. Costs around $0.10 per
    typical project lifecycle.

  - **ollama**: the local Qwen 2.5 Coder model on the Hetzner box.
    Free to run but slow and CPU-bound. Use this when you need the
    code to never leave your infrastructure (NDA'd customer projects,
    DoD work, etc.).

Selection
---------
The environment variable ``GUARDIAN_INDEXING_BACKEND`` controls the
choice. Valid values: ``claude-haiku`` (default), ``ollama``.

Anything else falls back to ``claude-haiku`` with a warning. We never
silently mismatch the user's intent — if you wrote a typo, the log
line tells you.

When the chosen backend can't actually be constructed (e.g. you
asked for ollama but no OLLAMA_BASE_URL is set), the factory returns
``None`` rather than crashing. Callers should treat ``None`` as
"indexing is disabled" — the rest of the pipeline already does this
because the existing code path tolerated ``ctx.ollama is None``.

Risk-analyzer note
------------------
Risk analysis uses its own factory in app.py (``_make_risk_client``)
because of the historical Railway env-var bug that forced it to
hardcode Claude. That factory and this one are separate by design —
indexing and risk reasoning could in principle use different
backends, and currently they're both Claude-by-default but for
different reasons. If we want to unify them later we can; doing it
now would conflate two independent decisions.
"""

from __future__ import annotations

import os
from typing import Any, Optional


_BACKEND_ENV = "GUARDIAN_INDEXING_BACKEND"
_BACKEND_CLAUDE = "claude-haiku"
_BACKEND_OLLAMA = "ollama"
_VALID_BACKENDS = {_BACKEND_CLAUDE, _BACKEND_OLLAMA}

# Default model when we pick the Claude backend. Pinned to Haiku 4.5
# specifically — Sonnet would also work but indexing is bulk per-file
# work where Haiku's speed advantage matters more than Sonnet's quality
# edge.
_CLAUDE_INDEXING_MODEL = "claude-haiku-4-5"


def chosen_backend() -> str:
    """Return the normalized backend name. Useful for log lines."""
    raw = os.environ.get(_BACKEND_ENV, "").strip().lower()
    if not raw:
        return _BACKEND_CLAUDE
    if raw not in _VALID_BACKENDS:
        print(f"[guardian_factory] unknown backend {raw!r}; "
              f"falling back to {_BACKEND_CLAUDE!r}. "
              f"Valid values: {sorted(_VALID_BACKENDS)}",
              flush=True)
        return _BACKEND_CLAUDE
    return raw


def make_guardian_client() -> Optional[Any]:
    """Construct the LLM client guardian indexing should use.

    Returns ``None`` when the chosen backend is unavailable — callers
    treat this as "indexing disabled" and skip guardian jobs gracefully.
    """
    backend = chosen_backend()

    if backend == _BACKEND_CLAUDE:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            print(f"[guardian_factory] backend=claude-haiku but "
                  f"ANTHROPIC_API_KEY is not set; "
                  f"guardian indexing will be disabled.",
                  flush=True)
            return None
        # Anthropic client supports per-call model overrides; we set
        # the default to Haiku here so every guardian call hits Haiku
        # without each call site having to pass model=. Other parts of
        # the codebase that construct AnthropicClient (e.g. the
        # Builder) get their own instance with the Sonnet default.
        from anthropic_client import AnthropicClient
        client = AnthropicClient(
            api_key=api_key,
            default_model=_CLAUDE_INDEXING_MODEL,
        )
        print(f"[guardian_factory] guardian indexing: claude "
              f"(model={_CLAUDE_INDEXING_MODEL})",
              flush=True)
        return client

    if backend == _BACKEND_OLLAMA:
        base_url = os.environ.get("OLLAMA_BASE_URL", "").strip()
        if not base_url:
            print(f"[guardian_factory] backend=ollama but "
                  f"OLLAMA_BASE_URL is not set; "
                  f"guardian indexing will be disabled.",
                  flush=True)
            return None
        # Reuse the same constructor logic the worker has used since
        # Turn A so behavior is identical to before the factory existed.
        from ollama_client import OllamaClient
        token = os.environ.get("OLLAMA_API_TOKEN") or None
        verify_tls = os.environ.get(
            "OLLAMA_VERIFY_TLS", "true",
        ).lower() not in ("false", "0", "no")
        try:
            timeout = float(os.environ.get("OLLAMA_TIMEOUT", "300"))
        except ValueError:
            timeout = 300.0
        default_model = (
            os.environ.get("OLLAMA_DEFAULT_MODEL", "").strip()
            or "qwen2.5-coder:7b"
        )
        client = OllamaClient(
            base_url=base_url,
            api_token=token,
            verify_tls=verify_tls,
            timeout_seconds=timeout,
            default_model=default_model,
        )
        print(f"[guardian_factory] guardian indexing: ollama "
              f"(base={base_url}, model={default_model})",
              flush=True)
        return client

    # Unreachable; chosen_backend() coerces to a valid value above.
    return None
