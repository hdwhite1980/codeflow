"""Tests for guardian_symbols (Turn B).

Python extraction uses stdlib ast — fully testable without external
deps. Tree-sitter languages get tested when the package is available
(production); we skip those tests gracefully when it's not installed
so CI without tree-sitter still passes.
"""

from __future__ import annotations

import json
import unittest

from guardian_symbols import (
    extract_guardian_symbols, extract_symbol_body,
    extract_symbols_python, supported_symbol_languages,
)

# Detect tree-sitter availability for conditional skipping.
try:
    import tree_sitter_languages  # noqa: F401
    HAS_TREESITTER = True
except ImportError:
    HAS_TREESITTER = False


class TestPythonExtraction(unittest.TestCase):
    def test_top_level_function(self):
        code = "def hello():\n    return 1\n"
        syms = extract_symbols_python(code)
        self.assertEqual(len(syms), 1)
        self.assertEqual(syms[0].kind, "function")
        self.assertEqual(syms[0].name, "hello")
        self.assertEqual(syms[0].qualified_name, "hello")
        self.assertEqual(syms[0].start_line, 1)

    def test_async_function(self):
        code = "async def fetch():\n    return None\n"
        syms = extract_symbols_python(code)
        self.assertEqual(len(syms), 1)
        self.assertEqual(syms[0].kind, "function")
        self.assertEqual(syms[0].name, "fetch")

    def test_class_with_methods(self):
        code = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        return 1\n"
            "    async def baz(self):\n"
            "        return 2\n"
        )
        syms = extract_symbols_python(code)
        # Expect class + 2 methods.
        kinds = [(s.kind, s.qualified_name) for s in syms]
        self.assertIn(("class", "Foo"), kinds)
        self.assertIn(("method", "Foo.bar"), kinds)
        self.assertIn(("method", "Foo.baz"), kinds)

    def test_uppercase_constant_extracted(self):
        code = "MAX_RETRIES = 5\n"
        syms = extract_symbols_python(code)
        self.assertEqual(len(syms), 1)
        self.assertEqual(syms[0].kind, "constant")
        self.assertEqual(syms[0].name, "MAX_RETRIES")

    def test_lowercase_top_level_not_extracted(self):
        """Lowercase bindings are noise — config dicts, helper vars."""
        code = "config = {'key': 'value'}\n"
        syms = extract_symbols_python(code)
        self.assertEqual(syms, [])

    def test_syntax_error_returns_empty(self):
        """Real-world partial generations — never raise."""
        code = "def broken(:\n    return\n"
        syms = extract_symbols_python(code)
        self.assertEqual(syms, [])

    def test_signature_extracted(self):
        code = "def add(a: int, b: int) -> int:\n    return a + b\n"
        syms = extract_symbols_python(code)
        self.assertEqual(len(syms), 1)
        self.assertIn("def add(a: int, b: int)", syms[0].signature)

    def test_end_line_correct(self):
        code = (
            "def a():\n"
            "    x = 1\n"
            "    return x\n"
            "\n"
            "def b():\n"
            "    return 2\n"
        )
        syms = extract_symbols_python(code)
        a = [s for s in syms if s.name == "a"][0]
        b = [s for s in syms if s.name == "b"][0]
        self.assertEqual(a.start_line, 1)
        self.assertEqual(a.end_line, 3)
        self.assertEqual(b.start_line, 5)


class TestExtractSymbolBody(unittest.TestCase):
    def test_extracts_correct_lines(self):
        code = (
            "def a():\n"
            "    return 1\n"
            "\n"
            "def b():\n"
            "    return 2\n"
        )
        syms = extract_symbols_python(code)
        b_sym = [s for s in syms if s.name == "b"][0]
        body = extract_symbol_body(code, b_sym)
        self.assertIn("def b()", body)
        self.assertIn("return 2", body)
        self.assertNotIn("def a()", body)


class TestDispatch(unittest.TestCase):
    def test_unknown_language_returns_empty(self):
        self.assertEqual(
            extract_guardian_symbols("hello", "klingon"), [],
        )

    def test_case_insensitive_dispatch(self):
        code = "def x(): pass\n"
        a = extract_guardian_symbols(code, "Python")
        b = extract_guardian_symbols(code, "PYTHON")
        self.assertEqual(len(a), 1)
        self.assertEqual(len(b), 1)

    def test_supported_languages_includes_all_targeted(self):
        langs = set(supported_symbol_languages())
        # Locking in the contract: this turn promised these languages.
        for required in (
            "python", "typescript", "tsx", "javascript", "jsx",
            "go", "rust", "java", "c", "cpp", "ruby", "php",
        ):
            self.assertIn(required, langs)


@unittest.skipUnless(HAS_TREESITTER, "tree-sitter-languages not installed")
class TestTreeSitterExtraction(unittest.TestCase):
    """Smoke tests that the per-language tree-sitter queries run without
    crashing and return at least one symbol on minimal valid programs.
    Detailed correctness (qualified names across all edge cases) we
    leave to integration testing on real repos — these tests guard
    against grammar version changes silently breaking extraction."""

    def test_typescript_function(self):
        syms = extract_guardian_symbols(
            "function hello(): void {}\n", "typescript",
        )
        names = [s.name for s in syms]
        self.assertIn("hello", names)

    def test_typescript_class_with_method(self):
        code = (
            "class Foo {\n"
            "  bar() { return 1; }\n"
            "}\n"
        )
        syms = extract_guardian_symbols(code, "typescript")
        kinds = [(s.kind, s.qualified_name) for s in syms]
        self.assertIn(("class", "Foo"), kinds)
        # Method should be qualified.
        method = [s for s in syms if s.kind == "method"]
        self.assertEqual(len(method), 1)
        self.assertEqual(method[0].qualified_name, "Foo.bar")

    def test_typescript_arrow_const(self):
        code = "const greet = (name: string) => `hi ${name}`;\n"
        syms = extract_guardian_symbols(code, "typescript")
        self.assertTrue(any(
            s.kind == "function" and s.name == "greet" for s in syms
        ))

    def test_typescript_uppercase_const(self):
        code = "const MAX_RETRIES = 5;\n"
        syms = extract_guardian_symbols(code, "typescript")
        self.assertTrue(any(
            s.kind == "constant" and s.name == "MAX_RETRIES" for s in syms
        ))

    def test_javascript_function(self):
        syms = extract_guardian_symbols(
            "function add(a, b) { return a + b; }\n", "javascript",
        )
        self.assertTrue(any(
            s.kind == "function" and s.name == "add" for s in syms
        ))

    def test_go_function(self):
        syms = extract_guardian_symbols(
            "package main\nfunc Hello() string { return \"hi\" }\n", "go",
        )
        self.assertTrue(any(
            s.kind == "function" and s.name == "Hello" for s in syms
        ))

    def test_rust_function(self):
        syms = extract_guardian_symbols(
            "fn add(a: i32, b: i32) -> i32 { a + b }\n", "rust",
        )
        self.assertTrue(any(
            s.kind == "function" and s.name == "add" for s in syms
        ))

    def test_java_class_with_method(self):
        code = (
            "class Foo {\n"
            "  public int bar() { return 1; }\n"
            "}\n"
        )
        syms = extract_guardian_symbols(code, "java")
        kinds = [(s.kind, s.name) for s in syms]
        self.assertIn(("class", "Foo"), kinds)
        self.assertIn(("method", "bar"), kinds)

    def test_ruby_class_with_method(self):
        code = (
            "class Foo\n"
            "  def bar\n"
            "    1\n"
            "  end\n"
            "end\n"
        )
        syms = extract_guardian_symbols(code, "ruby")
        self.assertTrue(any(s.kind == "class" for s in syms))

    def test_go_method_qualification(self):
        """Polish: Go methods should be qualified with their receiver
        type, e.g. method:Foo.Bar not method:Bar."""
        code = (
            "package main\n"
            "type Foo struct{}\n"
            "func (f *Foo) Bar() {}\n"
            "func (f Foo) Baz() {}\n"
            "func Bare() {}\n"
        )
        syms = extract_guardian_symbols(code, "go")
        methods = [s for s in syms if s.kind == "method"]
        method_qnames = sorted(s.qualified_name for s in methods)
        self.assertEqual(method_qnames, ["Foo.Bar", "Foo.Baz"])
        # The bare function shouldn't have any qualification.
        bare = [s for s in syms if s.kind == "function"]
        self.assertEqual(len(bare), 1)
        self.assertEqual(bare[0].qualified_name, "Bare")

    def test_truncated_source_returns_empty_or_partial(self):
        """Real-world: partial AI generations. Must not raise."""
        code = "function hello() {\n  if (x) {\n"  # unterminated
        # We don't assert what comes back; we just assert no crash.
        try:
            extract_guardian_symbols(code, "typescript")
        except Exception as exc:
            self.fail(f"Should not raise: {exc!r}")


if __name__ == "__main__":
    unittest.main()
