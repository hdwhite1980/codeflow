"""Tests for pricing.py — focus on Ollama models being known."""

from __future__ import annotations

import io
import sys
import unittest

from pricing import compute_costs, lookup_price


class TestOllamaPricing(unittest.TestCase):
    def test_qwen_7b_known(self):
        """Polish: qwen2.5-coder:7b should be in the pricing table so
        guardian indexing doesn't spam 'no price for model' warnings.

        Before this fix every guardian call produced a log line like
        `warning [pricing] no price for model 'qwen2.5-coder:7b'`."""
        self.assertIsNotNone(lookup_price("qwen2.5-coder:7b"))

    def test_qwen_14b_known(self):
        self.assertIsNotNone(lookup_price("qwen2.5-coder:14b"))

    def test_qwen_priced_at_zero(self):
        """Ollama runs on our hardware — recorded at zero dollar cost
        so the cost dashboard isn't polluted."""
        in_cost, out_cost = compute_costs(
            "qwen2.5-coder:7b", input_tokens=10_000, output_tokens=2_000,
        )
        self.assertEqual(in_cost, 0.0)
        self.assertEqual(out_cost, 0.0)

    def test_qwen_call_does_not_print_warning(self):
        """The regression we're guarding against: a quiet, unwarned
        zero-cost row for a known local model."""
        captured = io.StringIO()
        saved_stdout = sys.stdout
        sys.stdout = captured
        try:
            compute_costs(
                "qwen2.5-coder:7b", input_tokens=100, output_tokens=20,
            )
        finally:
            sys.stdout = saved_stdout
        self.assertNotIn("no price", captured.getvalue().lower())

    def test_truly_unknown_model_still_warns(self):
        """Make sure we didn't silence the helpful warning entirely —
        a model we genuinely don't know should still log."""
        captured = io.StringIO()
        saved_stdout = sys.stdout
        sys.stdout = captured
        try:
            compute_costs(
                "definitely-not-a-real-model-xyz",
                input_tokens=100, output_tokens=20,
            )
        finally:
            sys.stdout = saved_stdout
        self.assertIn("no price", captured.getvalue().lower())


class TestFrontierPricingRegression(unittest.TestCase):
    """Locking in that the existing frontier models still resolve."""

    def test_claude_sonnet_4_6(self):
        self.assertIsNotNone(lookup_price("claude-sonnet-4-6"))

    def test_gpt_5_4_mini(self):
        self.assertIsNotNone(lookup_price("gpt-5.4-mini"))


if __name__ == "__main__":
    unittest.main()
