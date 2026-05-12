"""Tests for the create-project request validation: service kinds,
non-empty services array, slug and prompt bounds.

We test the Pydantic models and the kind allowlist directly because
they're pure validation logic. The full POST endpoint requires a DB
and is exercised in integration via the production smoke tests.
"""

from __future__ import annotations

import os
import unittest

# Set minimal env before importing app so Settings() doesn't blow up at
# module import time.
os.environ.setdefault("DATABASE_URL", "postgresql://test:test@localhost/test")
os.environ.setdefault("REDIS_URL", "")
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-fake")

# Pydantic validation runs in the model __init__, so importing the
# models is enough to test them.
from app import (
    ALLOWED_SERVICE_KINDS,
    CreateProjectRequest,
    ServiceConfig,
)
from pydantic import ValidationError


class TestServiceConfig(unittest.TestCase):
    def test_minimal_valid_config(self):
        s = ServiceConfig(kind="postgres", label="Main DB")
        self.assertEqual(s.kind, "postgres")
        self.assertEqual(s.label, "Main DB")
        self.assertIsNone(s.config)

    def test_with_config_string(self):
        s = ServiceConfig(
            kind="railway",
            label="Production API",
            config="https://api.example.com",
        )
        self.assertEqual(s.config, "https://api.example.com")

    def test_label_required_nonempty(self):
        with self.assertRaises(ValidationError):
            ServiceConfig(kind="postgres", label="")

    def test_label_max_length(self):
        with self.assertRaises(ValidationError):
            ServiceConfig(kind="postgres", label="x" * 129)

    def test_config_max_length(self):
        with self.assertRaises(ValidationError):
            ServiceConfig(
                kind="postgres",
                label="db",
                config="x" * 2001,
            )


class TestCreateProjectRequest(unittest.TestCase):
    """The big one — make sure the new mandatory services field works."""

    def _services(self):
        """Build a minimal valid services list for reuse."""
        return [ServiceConfig(kind="postgres", label="Main DB")]

    def test_valid_request(self):
        req = CreateProjectRequest(
            slug="csv-converter",
            prompt="A FastAPI service that converts CSV to JSON.",
            services=self._services(),
        )
        self.assertEqual(req.slug, "csv-converter")
        self.assertEqual(len(req.services), 1)
        self.assertEqual(req.services[0].kind, "postgres")

    def test_services_required(self):
        with self.assertRaises(ValidationError) as ctx:
            CreateProjectRequest(
                slug="csv-converter",
                prompt="A FastAPI service that converts CSV to JSON.",
            )  # type: ignore[call-arg]
        # The error should mention 'services'.
        self.assertIn("services", str(ctx.exception).lower())

    def test_empty_services_rejected(self):
        with self.assertRaises(ValidationError):
            CreateProjectRequest(
                slug="csv-converter",
                prompt="A FastAPI service that converts CSV to JSON.",
                services=[],
            )

    def test_max_20_services(self):
        many = [
            ServiceConfig(kind="postgres", label=f"db-{i}")
            for i in range(21)
        ]
        with self.assertRaises(ValidationError):
            CreateProjectRequest(
                slug="csv-converter",
                prompt="Test prompt long enough.",
                services=many,
            )

    def test_slug_min_length(self):
        with self.assertRaises(ValidationError):
            CreateProjectRequest(
                slug="x",
                prompt="A FastAPI service that converts CSV to JSON.",
                services=self._services(),
            )

    def test_prompt_min_length(self):
        with self.assertRaises(ValidationError):
            CreateProjectRequest(
                slug="csv-converter",
                prompt="short",
                services=self._services(),
            )


class TestAllowedServiceKinds(unittest.TestCase):
    """The Pydantic model allows any string for `kind`; the validation
    against the allowlist happens in the route handler. We test the
    allowlist contents directly to catch regressions when someone adds
    a kind to the frontend without updating the backend list."""

    def test_expected_kinds_present(self):
        expected = {
            "postgres", "mysql", "redis",
            "railway", "vercel",
            "github",
            "stripe",
            "anthropic", "openai",
            "other",
        }
        self.assertTrue(expected.issubset(ALLOWED_SERVICE_KINDS))

    def test_other_is_present_as_escape_hatch(self):
        self.assertIn("other", ALLOWED_SERVICE_KINDS)


class TestGraphHelpers(unittest.TestCase):
    """The /graph endpoint uses a couple of small helper functions to
    bucket files into groups. They're pure functions and easy to pin."""

    def test_folder_of_nested(self):
        from app import _folder_of
        self.assertEqual(_folder_of("app/main.py"), "app")
        self.assertEqual(_folder_of("app/routers/convert.py"), "app/routers")
        self.assertEqual(_folder_of("tests/test_main.py"), "tests")

    def test_folder_of_root(self):
        from app import _folder_of
        self.assertEqual(_folder_of("README.md"), "root")
        self.assertEqual(_folder_of("requirements.txt"), "root")


if __name__ == "__main__":
    unittest.main()
