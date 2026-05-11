"""
codeflow.pricing
================

Per-million-token pricing for every model we call. Used by the usage
recorder to compute dollar costs at write time, so historical rows lock
in what we actually paid even if prices change later.

How prices are listed
---------------------
Each entry is `(input_per_mtok_usd, output_per_mtok_usd)` — dollars per
1,000,000 tokens of that type. This matches how both Anthropic and
OpenAI publish their prices, so you can copy numbers off the docs page
without conversion.

When to update this file
------------------------
* When a provider changes prices on a model we use.
* When we adopt a new model.

Do NOT change a price for a model we already use in production unless
we're literally being charged the new rate as of today. Past `token_usage`
rows already locked in the old number — that's correct behavior. The
table reflects "what we'll be charged for the next call we make."

Snapshot-aware lookup
---------------------
Some model IDs come back from the API with a date snapshot suffix
(e.g. `gpt-5.4-mini-2026-03-17` rather than `gpt-5.4-mini`). We strip
trailing date-like suffixes before lookup so we don't have to enumerate
every snapshot. If the bare name isn't found either, the call returns
zeros and logs a warning — usage gets recorded, just without a price.
Better than silently dropping the row.
"""

from __future__ import annotations

import re
from typing import Optional


# Prices in USD per 1,000,000 tokens. (input_per_mtok, output_per_mtok)
# Source of truth: each provider's public pricing page. Last updated
# 2026-05; revisit if a provider rotates prices on a model.
PRICING_PER_MTOK: dict[str, tuple[float, float]] = {
    # --- Anthropic ---
    # Sonnet 4.6 is what the build pipeline uses by default.
    "claude-sonnet-4-6":   (3.00,  15.00),
    "claude-opus-4-7":     (5.00,  25.00),
    "claude-opus-4-6":     (5.00,  25.00),
    "claude-haiku-4-5":    (1.00,   5.00),

    # --- OpenAI ---
    # gpt-5.4-mini is what the audit pipeline uses by default.
    "gpt-5.5":             (5.00,  30.00),
    "gpt-5.4":             (2.50,  15.00),
    "gpt-5.4-mini":        (0.75,   4.50),
    "gpt-5.4-nano":        (0.20,   1.25),
    "gpt-5":               (1.25,  10.00),
    "gpt-5-mini":          (0.25,   2.00),
}


# Pattern that matches a trailing date suffix on OpenAI model IDs.
# e.g. "gpt-5.4-mini-2026-03-17" → strip "-2026-03-17".
_DATE_SUFFIX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}$")


def _normalize_model(model: str) -> str:
    """Strip date snapshot suffixes for table lookup."""
    return _DATE_SUFFIX_RE.sub("", model)


def lookup_price(model: str) -> Optional[tuple[float, float]]:
    """Return (input_per_mtok, output_per_mtok) in USD, or None if unknown.

    Tries the exact model name first, then the normalized form with any
    trailing date suffix removed. Returning None (not raising) keeps the
    usage path resilient — we'd rather log a missing-price warning and
    record zero cost than fail the whole build because we added a new
    model without updating the table."""
    if model in PRICING_PER_MTOK:
        return PRICING_PER_MTOK[model]
    normalized = _normalize_model(model)
    if normalized in PRICING_PER_MTOK:
        return PRICING_PER_MTOK[normalized]
    return None


def compute_costs(
    model: str, input_tokens: int, output_tokens: int,
) -> tuple[float, float]:
    """Return (input_cost_usd, output_cost_usd) for one API call.

    If the model is unknown, returns (0.0, 0.0) and prints a one-line
    warning. The unknown-model case is recoverable: usage rows still
    record tokens, just with $0 cost. Once the model is added to the
    table, future calls compute correctly."""
    price = lookup_price(model)
    if price is None:
        print(f"[pricing] no price for model {model!r}; recording zero cost. "
              f"Update codeflow.pricing.PRICING_PER_MTOK.")
        return 0.0, 0.0
    in_per_mtok, out_per_mtok = price
    input_cost = (input_tokens / 1_000_000.0) * in_per_mtok
    output_cost = (output_tokens / 1_000_000.0) * out_per_mtok
    return input_cost, output_cost
