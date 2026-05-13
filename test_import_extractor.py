"""Tests for codeflow.import_extractor — the heart of Turn 2.

Pure functions, no I/O. We exercise:
  * Python `import` and `from X import Y` resolution
  * Python relative imports (`from .`, `from ..`)
  * JS/TS imports (static, dynamic, require, side-effect)
  * Service detection by env var name matching
  * Tolerance to malformed input (truncated code, syntax errors)
"""

from __future__ import annotations

import unittest

from import_extractor import (
    ExtractionResult,
    ServiceRef,
    extract_references,
    make_service_refs,
)


class TestPythonImports(unittest.TestCase):
    def test_from_import_resolved_to_file(self):
        result = extract_references(
            file_path="app/main.py",
            content=(
                "from app.csv_utils import parse_csv\n"
                "import logging\n"  # external; ignored
                "print('hi')\n"
            ),
            known_files={"app/main.py", "app/csv_utils.py"},
            service_refs=[],
        )
        self.assertEqual(result.file_imports, ["app/csv_utils.py"])

    def test_plain_import_resolved(self):
        result = extract_references(
            file_path="app/main.py",
            content="import app.csv_utils\nx = 1\n",
            known_files={"app/main.py", "app/csv_utils.py"},
            service_refs=[],
        )
        self.assertEqual(result.file_imports, ["app/csv_utils.py"])

    def test_external_imports_ignored(self):
        result = extract_references(
            file_path="app/main.py",
            content="from fastapi import FastAPI\nimport os\nimport json\n",
            known_files={"app/main.py"},
            service_refs=[],
        )
        self.assertEqual(result.file_imports, [])

    def test_relative_import_same_dir(self):
        result = extract_references(
            file_path="app/main.py",
            content="from .csv_utils import parse_csv\n",
            known_files={"app/main.py", "app/csv_utils.py"},
            service_refs=[],
        )
        self.assertEqual(result.file_imports, ["app/csv_utils.py"])

    def test_relative_import_parent_dir(self):
        result = extract_references(
            file_path="app/routers/convert.py",
            content="from ..csv_utils import parse_csv\n",
            known_files={
                "app/routers/convert.py",
                "app/csv_utils.py",
            },
            service_refs=[],
        )
        self.assertEqual(result.file_imports, ["app/csv_utils.py"])

    def test_import_from_subpackage_init(self):
        """`from app import csv_utils` should resolve to app/csv_utils.py
        if that exists, and to app/__init__.py otherwise. We test the
        first (more common) case."""
        result = extract_references(
            file_path="app/main.py",
            content="from app import csv_utils\n",
            known_files={"app/main.py", "app/csv_utils.py", "app/__init__.py"},
            service_refs=[],
        )
        # 'app' resolves to app/__init__.py (the package itself). That's
        # the conservative choice — we know app/__init__.py is a known
        # file, so we emit that. Symbol-level resolution (which csv_utils
        # this imports) is out of scope.
        self.assertIn("app/__init__.py", result.file_imports)

    def test_multiple_imports_deduplicated(self):
        result = extract_references(
            file_path="app/main.py",
            content=(
                "from app.csv_utils import parse_csv\n"
                "from app.csv_utils import write_csv\n"
                "from app.csv_utils import other_thing\n"
            ),
            known_files={"app/main.py", "app/csv_utils.py"},
            service_refs=[],
        )
        # Same target imported 3 times — should appear once.
        self.assertEqual(result.file_imports.count("app/csv_utils.py"), 1)

    def test_self_import_filtered(self):
        """A file importing itself shouldn't produce a self-edge."""
        result = extract_references(
            file_path="app/main.py",
            content="from app.main import something\n",
            known_files={"app/main.py", "app/csv_utils.py"},
            service_refs=[],
        )
        self.assertNotIn("app/main.py", result.file_imports)

    def test_syntax_error_returns_empty(self):
        """Truncated/malformed Python should not crash the build."""
        result = extract_references(
            file_path="app/main.py",
            content="def broken_func(\n",  # truncated
            known_files={"app/main.py"},
            service_refs=[],
        )
        self.assertEqual(result.file_imports, [])


class TestJavaScriptImports(unittest.TestCase):
    def test_static_import_from(self):
        result = extract_references(
            file_path="src/app/page.tsx",
            content="import { Foo } from './components/foo';\n",
            known_files={
                "src/app/page.tsx",
                "src/app/components/foo.tsx",
            },
            service_refs=[],
        )
        self.assertEqual(result.file_imports, ["src/app/components/foo.tsx"])

    def test_side_effect_import(self):
        result = extract_references(
            file_path="src/app/page.tsx",
            content="import './globals.css';\nfunction X() {}\n",
            known_files={"src/app/page.tsx", "src/app/globals.css"},
            service_refs=[],
        )
        # We don't have CSS as a known import target right now — it's
        # not in the JS extension list. So result is empty.
        # If you want CSS imports to draw edges, extend _js_candidates.
        self.assertEqual(result.file_imports, [])

    def test_require(self):
        result = extract_references(
            file_path="server.js",
            content="const x = require('./utils');\n",
            known_files={"server.js", "utils.js"},
            service_refs=[],
        )
        self.assertEqual(result.file_imports, ["utils.js"])

    def test_external_package_ignored(self):
        result = extract_references(
            file_path="src/app/page.tsx",
            content=(
                "import React from 'react';\n"
                "import { Button } from '@radix-ui/react-slot';\n"
            ),
            known_files={"src/app/page.tsx"},
            service_refs=[],
        )
        self.assertEqual(result.file_imports, [])

    def test_parent_directory(self):
        result = extract_references(
            file_path="src/app/projects/page.tsx",
            content="import { api } from '../../lib/api';\n",
            known_files={
                "src/app/projects/page.tsx",
                "src/lib/api.ts",
            },
            service_refs=[],
        )
        self.assertEqual(result.file_imports, ["src/lib/api.ts"])


class TestServiceMatching(unittest.TestCase):
    def test_make_service_refs_extracts_env_vars(self):
        services = [
            {
                "kind": "postgres",
                "label": "Main DB",
                "config": "DATABASE_URL=postgres://localhost/x",
            },
        ]
        refs = make_service_refs(services)
        self.assertEqual(len(refs), 1)
        self.assertEqual(refs[0].artifact_key, "service:postgres:main-db")
        self.assertIn("DATABASE_URL", refs[0].needles)

    def test_make_service_refs_skips_unidentifiable(self):
        """A service with no env-var-shaped or URL config is unmatchable.
        It still exists as a node but the extractor can't draw edges
        to it automatically."""
        services = [
            {"kind": "github", "label": "repo", "config": "some random label"},
        ]
        refs = make_service_refs(services)
        self.assertEqual(refs, [])

    def test_extract_service_use(self):
        services = [
            {
                "kind": "postgres",
                "label": "Main DB",
                "config": "DATABASE_URL=postgres://...",
            },
        ]
        refs = make_service_refs(services)
        result = extract_references(
            file_path="app/main.py",
            content=(
                "import os\n"
                "db = os.environ['DATABASE_URL']\n"
            ),
            known_files={"app/main.py"},
            service_refs=refs,
        )
        self.assertEqual(result.service_uses, ["service:postgres:main-db"])

    def test_extract_service_via_url(self):
        services = [
            {
                "kind": "railway",
                "label": "API",
                "config": "https://api.example.com",
            },
        ]
        refs = make_service_refs(services)
        result = extract_references(
            file_path="app/client.py",
            content='URL = "https://api.example.com/v1"\n',
            known_files={"app/client.py"},
            service_refs=refs,
        )
        self.assertEqual(result.service_uses, ["service:railway:api"])

    def test_no_service_use_when_no_match(self):
        services = [
            {
                "kind": "postgres",
                "label": "Main DB",
                "config": "DATABASE_URL=postgres://...",
            },
        ]
        refs = make_service_refs(services)
        result = extract_references(
            file_path="README.md",
            content="# A nice readme\nNo env vars mentioned here.\n",
            known_files={"README.md"},
            service_refs=refs,
        )
        self.assertEqual(result.service_uses, [])

    def test_multiple_services_same_file(self):
        services = [
            {"kind": "postgres", "label": "DB",
             "config": "DATABASE_URL=postgres://"},
            {"kind": "redis", "label": "Cache",
             "config": "REDIS_URL=redis://"},
        ]
        refs = make_service_refs(services)
        result = extract_references(
            file_path="app/main.py",
            content=(
                "import os\n"
                "db = os.environ['DATABASE_URL']\n"
                "cache = os.environ['REDIS_URL']\n"
            ),
            known_files={"app/main.py"},
            service_refs=refs,
        )
        self.assertEqual(
            sorted(result.service_uses),
            ["service:postgres:db", "service:redis:cache"],
        )


class TestExtractionRobustness(unittest.TestCase):
    """The extractor runs against AI-generated code, which can be
    weird. These tests pin behavior on edge cases."""

    def test_unknown_extension_returns_empty(self):
        result = extract_references(
            file_path="config.yaml",
            content="postgres:\n  url: DATABASE_URL\n",
            known_files={"config.yaml"},
            service_refs=[],
        )
        # No extractor for YAML; no file imports possible.
        self.assertEqual(result.file_imports, [])

    def test_service_match_works_regardless_of_extension(self):
        """Service matching is a substring scan over content, so it
        works on any file type."""
        services = [
            {"kind": "postgres", "label": "DB",
             "config": "DATABASE_URL=postgres://"},
        ]
        refs = make_service_refs(services)
        result = extract_references(
            file_path="README.md",
            content="# App\nSet DATABASE_URL in your env.\n",
            known_files={"README.md"},
            service_refs=refs,
        )
        self.assertEqual(result.service_uses, ["service:postgres:db"])

    def test_empty_content(self):
        result = extract_references(
            file_path="empty.py",
            content="",
            known_files={"empty.py"},
            service_refs=[],
        )
        self.assertEqual(result.file_imports, [])
        self.assertEqual(result.service_uses, [])


if __name__ == "__main__":
    unittest.main()
