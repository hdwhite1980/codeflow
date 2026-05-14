"""Tests for the language-expansion changes (Turn G-language).

Covers:
  - _language_of for all newly-recognized extensions
  - _language_from_extension parity (job_handlers version)
  - _language_audit_block returns concrete content for each new lang
  - _audit_user_prompt embeds the language block when language matches
"""

from __future__ import annotations

import unittest

from audit_pipeline import (
    _audit_user_prompt, _language_audit_block,
)
from iterate_pipeline import _language_of
from job_handlers import _language_from_extension


class TestLanguageOfNewExtensions(unittest.TestCase):
    def test_powershell_script(self):
        self.assertEqual(_language_of("scripts/audit.ps1"), "powershell")

    def test_powershell_module(self):
        self.assertEqual(_language_of("src/ConditionalAccess.psm1"), "powershell")

    def test_powershell_manifest(self):
        self.assertEqual(_language_of("src/ConditionalAccess.psd1"), "powershell")

    def test_kql_extension(self):
        self.assertEqual(_language_of("queries/failed-mfa.kql"), "kql")

    def test_csl_extension(self):
        self.assertEqual(_language_of("queries/legacy.csl"), "kql")

    def test_bash_sh(self):
        self.assertEqual(_language_of("deploy.sh"), "bash")

    def test_bash_explicit_extension(self):
        self.assertEqual(_language_of("helpers.bash"), "bash")

    def test_zsh_treated_as_bash(self):
        # Audit concerns are identical across POSIX shells; we tag them
        # as one language to share the audit block.
        self.assertEqual(_language_of("setup.zsh"), "bash")

    def test_applescript_source(self):
        self.assertEqual(
            _language_of("Scripts/AdoBuilder.applescript"), "applescript",
        )

    def test_applescript_compiled(self):
        self.assertEqual(_language_of("Library/macro.scpt"), "applescript")

    def test_case_insensitive(self):
        # File extensions can be uppercase on some macOS systems / SMB shares.
        self.assertEqual(_language_of("Module.PSM1"), "powershell")
        self.assertEqual(_language_of("Query.KQL"), "kql")

    def test_unrecognized_returns_text(self):
        self.assertEqual(_language_of("README"), "text")
        self.assertEqual(_language_of("notes.xyz"), "text")

    def test_existing_languages_still_detected(self):
        # Regression: existing language mappings must not break.
        self.assertEqual(_language_of("app/main.py"), "python")
        self.assertEqual(_language_of("src/App.tsx"), "typescript")
        self.assertEqual(_language_of("config.yaml"), "yaml")


class TestLanguageFromExtensionParity(unittest.TestCase):
    """The job_handlers._language_from_extension function feeds the
    guardian indexer prompt. It should agree with iterate's _language_of
    for the new languages — otherwise the audit auditor and the
    guardian indexer would tag the same file differently."""

    def test_powershell_parity(self):
        for path in ("a.ps1", "b.psm1", "c.psd1", "d.ps1xml"):
            self.assertEqual(
                _language_from_extension(path), "powershell", msg=path,
            )

    def test_kql_parity(self):
        for path in ("a.kql", "b.csl"):
            self.assertEqual(_language_from_extension(path), "kql", msg=path)

    def test_bash_parity(self):
        for path in ("a.sh", "b.bash", "c.zsh"):
            self.assertEqual(_language_from_extension(path), "bash", msg=path)

    def test_applescript_parity(self):
        for path in ("a.applescript", "b.scpt"):
            self.assertEqual(
                _language_from_extension(path), "applescript", msg=path,
            )


class TestLanguageAuditBlocks(unittest.TestCase):
    def test_powershell_block_mentions_key_concerns(self):
        block = _language_audit_block("powershell", "x.ps1")
        # Spot-check the high-signal concerns are present.
        self.assertIn("CmdletBinding", block)
        self.assertIn("Mandatory", block)
        self.assertIn("SecureString", block)
        self.assertIn("Connect-MgGraph", block)
        self.assertIn("Try/Catch", block)

    def test_bash_block_mentions_key_concerns(self):
        block = _language_audit_block("bash", "x.sh")
        self.assertIn("set -euo pipefail", block)
        self.assertIn("rm -rf", block)
        self.assertIn("eval", block)
        self.assertIn("Unquoted", block)

    def test_kql_block_mentions_tenant_scoping(self):
        block = _language_audit_block("kql", "q.kql")
        self.assertIn("tenant", block.lower())
        self.assertIn("CRITICAL", block)  # tenant leak is critical
        self.assertIn("where", block)
        self.assertIn("join", block)

    def test_applescript_block_mentions_permissions(self):
        block = _language_audit_block("applescript", "x.applescript")
        self.assertIn("Accessibility", block)
        self.assertIn("System Events", block)
        self.assertIn("try", block)

    def test_unknown_language_returns_empty(self):
        self.assertEqual(_language_audit_block("python", "x.py"), "")
        self.assertEqual(_language_audit_block("rust", "x.rs"), "")
        self.assertEqual(_language_audit_block("", "x"), "")

    def test_block_case_insensitive(self):
        self.assertIn("CmdletBinding", _language_audit_block("PowerShell", "x.ps1"))
        self.assertIn("CmdletBinding", _language_audit_block("POWERSHELL", "x.ps1"))


class TestAuditPromptIncludesLanguageBlock(unittest.TestCase):
    def test_powershell_file_gets_powershell_block(self):
        prompt = _audit_user_prompt(
            file_path="src/Audit.psm1",
            file_content="function Get-Foo {}",
            purpose="Audit Conditional Access policies",
            language="powershell",
        )
        self.assertIn("CmdletBinding", prompt)
        self.assertIn("Connect-MgGraph", prompt)

    def test_kql_file_gets_kql_block(self):
        prompt = _audit_user_prompt(
            file_path="queries/x.kql",
            file_content="SigninLogs | where ...",
            purpose="Detect failed MFA",
            language="kql",
        )
        self.assertIn("tenant", prompt.lower())

    def test_python_file_no_msp_blocks(self):
        """Regression: a Python audit must NOT contain PowerShell or KQL
        guidance. False-positive concern dilution would degrade audits."""
        prompt = _audit_user_prompt(
            file_path="app/main.py",
            file_content="def main(): pass",
            purpose="Entry point",
            language="python",
        )
        self.assertNotIn("CmdletBinding", prompt)
        self.assertNotIn("Connect-MgGraph", prompt)
        # KQL-specific terms shouldn't appear either.
        self.assertNotIn("SigninLogs", prompt)

    def test_general_checklist_still_present(self):
        """Language block additive — doesn't replace the general checklist."""
        prompt = _audit_user_prompt(
            file_path="x.ps1", file_content="x", purpose="x",
            language="powershell",
        )
        # General categories are still in there.
        self.assertIn("security", prompt.lower())
        self.assertIn("error handling", prompt.lower())


if __name__ == "__main__":
    unittest.main()
