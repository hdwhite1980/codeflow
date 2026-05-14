"""Tests for symbol-aware risk analyzer (Turn B (b) — frontend + risk
analyzer follow-up).

We exercise the prompt builder directly so we don't need a live Ollama
or Claude call. The end-to-end run through analyze_change_risk needs
an actual model; that's verified manually against production.
"""

from __future__ import annotations

import unittest

from guardian_pipeline import _build_risk_prompt


class TestRiskPromptIncludesSymbols(unittest.TestCase):
    def _file_summary(self, path: str, **overrides):
        base = {
            "file_path": path,
            "purpose": f"purpose of {path}",
            "touches": ["state-a"],
            "assumes": ["assumption-a"],
            "failure_modes": ["failure-a"],
            "risk_notes": ["existing risk note"],
        }
        base.update(overrides)
        return base

    def _symbol(self, qname, **overrides):
        base = {
            "qualified_name": qname,
            "symbol_kind": "function",
            "purpose": f"purpose of {qname}",
            "plain_english": f"plain {qname}",
            "risk_notes": [],
        }
        base.update(overrides)
        return base

    def test_prompt_without_symbols_has_no_symbol_block(self):
        """Regression: passing no symbol_summaries should produce the
        same prompt shape as before Turn B."""
        prompt = _build_risk_prompt(
            target="app/main.py",
            change_description="rename function foo",
            impact_set=["app/main.py", "app/util.py"],
            summaries={"app/main.py": self._file_summary("app/main.py")},
        )
        self.assertIn("app/main.py", prompt)
        self.assertNotIn("Symbols (", prompt)

    def test_prompt_with_symbols_lists_them_under_file(self):
        prompt = _build_risk_prompt(
            target="app/auth.py",
            change_description="drop verify_password",
            impact_set=["app/auth.py"],
            summaries={"app/auth.py": self._file_summary("app/auth.py")},
            symbol_summaries={
                "app/auth.py": [
                    self._symbol("verify_password",
                                 purpose="compare plaintext to bcrypt hash"),
                    self._symbol("hash_password", purpose="bcrypt wrapper"),
                    self._symbol("AuthService.login", symbol_kind="method"),
                ]
            },
        )
        self.assertIn("Symbols (3)", prompt)
        self.assertIn("verify_password", prompt)
        self.assertIn("[function] verify_password", prompt)
        self.assertIn("AuthService.login", prompt)
        self.assertIn("compare plaintext to bcrypt hash", prompt)

    def test_prompt_with_symbols_includes_citing_instruction(self):
        prompt = _build_risk_prompt(
            target="x", change_description="y", impact_set=[],
            summaries={"x": self._file_summary("x")},
            symbol_summaries={"x": [self._symbol("foo")]},
        )
        self.assertIn("citing", prompt.lower())
        self.assertIn("specific symbol", prompt.lower())

    def test_prompt_caps_symbols_at_15_per_file(self):
        many = [self._symbol(f"fn_{i}") for i in range(50)]
        prompt = _build_risk_prompt(
            target="x", change_description="y", impact_set=[],
            summaries={"app/x.py": self._file_summary("app/x.py")},
            symbol_summaries={"app/x.py": many},
        )
        # Cap message is present.
        self.assertIn("... and 35 more symbols", prompt)
        # First 15 included, 16th not (looking for fn_15 since 0-indexed).
        self.assertIn("fn_0", prompt)
        self.assertIn("fn_14", prompt)
        self.assertNotIn("fn_15 —", prompt)

    def test_prompt_with_symbol_risk_notes_includes_risk_line(self):
        prompt = _build_risk_prompt(
            target="x", change_description="y", impact_set=[],
            summaries={"x.py": self._file_summary("x.py")},
            symbol_summaries={"x.py": [
                self._symbol(
                    "danger_fn",
                    risk_notes=["catches generic exception, swallows errors"],
                ),
            ]},
        )
        self.assertIn("danger_fn", prompt)
        self.assertIn("catches generic exception", prompt)
        self.assertIn("risk:", prompt)


if __name__ == "__main__":
    unittest.main()
