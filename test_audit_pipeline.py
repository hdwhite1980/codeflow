"""
test_audit_pipeline
===================

Tests for the OpenAI-backed audit step. Uses a fake OpenAI client that
returns scripted responses; no real API calls.

Coverage
--------
* Parsing valid findings JSON
* Rejecting malformed findings shapes
* Sanitizing individual bad finding entries (drop, don't fail)
* Severity/category counts in the outcome
* Auth-error early abort
* Missing-key skip-record behavior at the handler level
* Build → audit chaining via the queue
"""

from __future__ import annotations

import asyncio
import json
import unittest
from typing import Any, Optional

from audit_pipeline import (
    AuditOutcome, audit_verdict_key, run_audit,
    _validate_findings, _extract_json,
)
from job_handlers import HandlerContext, dispatch
from jobqueue import MemoryJobQueue, make_job
from ledger_memory import InMemoryLedgerStore
from openai_client import CompletionResult, OpenAIError


def _run(coro):
    return asyncio.run(coro)


class FakeOpenAIClient:
    """Mimics OpenAIClient.complete() with a queue of canned responses."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def complete(
        self, prompt, *, system=None, model=None,
        max_tokens=2048, temperature=0.2, json_mode=False,
    ):
        self.calls.append({
            "prompt": prompt, "system": system, "model": model,
            "max_tokens": max_tokens, "temperature": temperature,
            "json_mode": json_mode,
        })
        if not self._responses:
            raise AssertionError(
                f"FakeOpenAIClient ran out of scripted responses; "
                f"call #{len(self.calls)} had no script."
            )
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        if isinstance(nxt, CompletionResult):
            return nxt
        return CompletionResult(
            text=nxt, model="fake-mini", finish_reason="stop",
            input_tokens=300, output_tokens=80,
        )

    async def aclose(self):
        pass


def findings_json(*findings):
    return json.dumps({"findings": list(findings)})


def crit(issue, line=None, fix=None):
    return {
        "severity": "critical",
        "category": "correctness",
        "line": line,
        "issue": issue,
        "suggested_fix": fix,
    }


def warn(issue, category="correctness", line=None):
    return {
        "severity": "warning",
        "category": category,
        "line": line,
        "issue": issue,
        "suggested_fix": None,
    }


def nit(issue):
    return {
        "severity": "nit",
        "category": "style",
        "line": None,
        "issue": issue,
        "suggested_fix": None,
    }


# ---------------------------------------------------------------------------
# Parser tests.
# ---------------------------------------------------------------------------

class TestValidateFindings(unittest.TestCase):
    def test_clean_empty_list(self):
        out = _validate_findings({"findings": []})
        self.assertEqual(out, [])

    def test_passes_normal_findings(self):
        raw = {"findings": [crit("bug here", line=42, fix="do this")]}
        out = _validate_findings(raw)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["severity"], "critical")
        self.assertEqual(out[0]["line"], 42)

    def test_drops_finding_with_bad_severity(self):
        raw = {"findings": [
            crit("legit"),
            {"severity": "WAT", "category": "correctness",
             "line": None, "issue": "x"},
        ]}
        out = _validate_findings(raw)
        self.assertEqual(len(out), 1)

    def test_unknown_category_becomes_other(self):
        raw = {"findings": [
            {"severity": "warning", "category": "esoteric",
             "line": None, "issue": "weird thing",
             "suggested_fix": None},
        ]}
        out = _validate_findings(raw)
        self.assertEqual(out[0]["category"], "other")

    def test_non_string_line_becomes_none(self):
        raw = {"findings": [
            {"severity": "warning", "category": "style",
             "line": "ten", "issue": "x",
             "suggested_fix": None},
        ]}
        out = _validate_findings(raw)
        self.assertIsNone(out[0]["line"])

    def test_empty_issue_dropped(self):
        raw = {"findings": [
            {"severity": "warning", "category": "style",
             "line": None, "issue": "  ",
             "suggested_fix": None},
        ]}
        out = _validate_findings(raw)
        self.assertEqual(out, [])

    def test_truncates_excessive_findings(self):
        raw = {"findings": [nit(f"thing {i}") for i in range(200)]}
        out = _validate_findings(raw)
        self.assertEqual(len(out), 100)

    def test_missing_findings_key_returns_none(self):
        out = _validate_findings({"verdict": "ok"})
        self.assertIsNone(out)


class TestExtractJson(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(_extract_json('{"a": 1}'), {"a": 1})

    def test_json_with_fences(self):
        text = "```json\n" + '{"findings": []}' + "\n```"
        self.assertEqual(_extract_json(text), {"findings": []})

    def test_garbage_returns_none(self):
        self.assertIsNone(_extract_json("not json at all"))


# ---------------------------------------------------------------------------
# Pipeline tests.
# ---------------------------------------------------------------------------

class TestRunAudit(unittest.TestCase):
    def _make_file_artifacts(self, *names):
        return [
            {
                "path": n,
                "content": f"# placeholder for {n}\n",
                "purpose": f"do {n}",
                "language": "python",
            }
            for n in names
        ]

    def test_clean_audit_writes_empty_verdict(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("clean", "test clean audit")
        client = FakeOpenAIClient([findings_json()])
        outcome = _run(run_audit(
            project_id=pid,
            file_artifacts=self._make_file_artifacts("a.py"),
            client=client, store=store,
        ))
        self.assertTrue(outcome.all_clean)
        self.assertEqual(outcome.total_findings, 0)
        # Verdict entry should still be written.
        key = audit_verdict_key(pid, "a.py", "openai")
        entry = store.current_entry(pid, key)
        self.assertIsNotNone(entry)

    def test_findings_counted_by_severity(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("multi", "test severity counts")
        client = FakeOpenAIClient([
            findings_json(crit("bug a"), warn("smell a")),
            findings_json(nit("style b"), nit("nit b")),
        ])
        outcome = _run(run_audit(
            project_id=pid,
            file_artifacts=self._make_file_artifacts("a.py", "b.py"),
            client=client, store=store,
        ))
        self.assertFalse(outcome.all_clean)
        self.assertEqual(outcome.critical_count, 1)
        self.assertEqual(outcome.warning_count, 1)
        self.assertEqual(outcome.nit_count, 2)
        self.assertEqual(outcome.total_findings, 4)
        self.assertEqual(outcome.audited_files, ["a.py", "b.py"])

    def test_verdict_body_records_file_path(self):
        """Audit verdicts are ledger-only (not graph nodes), so the
        connection back to the file is via file_path in the body. The
        UI joins verdicts to files using that. Lock this in so the
        contract stays stable for the frontend."""
        store = InMemoryLedgerStore()
        pid = store.create_project("body", "test verdict body shape")
        client = FakeOpenAIClient([findings_json(warn("trivial"))])
        _run(run_audit(
            project_id=pid,
            file_artifacts=self._make_file_artifacts("only.py"),
            client=client, store=store,
        ))
        verdict_key = audit_verdict_key(pid, "only.py", "openai")
        entry = store.current_entry(pid, verdict_key)
        self.assertIsNotNone(entry)
        blob, _ = store.get_blob(entry.blob_sha256)
        body = json.loads(blob.decode())
        self.assertEqual(body["file_path"], "only.py")
        self.assertEqual(body["auditor"], "openai")
        self.assertEqual(len(body["findings"]), 1)

    def test_parse_failure_writes_verdict_with_flag(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("badparse", "test parse failure")
        client = FakeOpenAIClient([
            "this is not JSON",
            "still not JSON",
        ])
        outcome = _run(run_audit(
            project_id=pid,
            file_artifacts=self._make_file_artifacts("a.py"),
            client=client, store=store,
        ))
        # The audit ran, didn't raise. Findings empty.
        self.assertEqual(outcome.total_findings, 0)
        self.assertIn("a.py", outcome.audited_files)
        # And the verdict entry should be flagged.
        entry = store.current_entry(pid, audit_verdict_key(pid, "a.py", "openai"))
        self.assertIsNotNone(entry)
        blob, _ = store.get_blob(entry.blob_sha256)
        body = json.loads(blob.decode())
        self.assertTrue(body.get("parse_error"))

    def test_auth_error_aborts_remaining(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("auth", "test auth fail")
        client = FakeOpenAIClient([
            findings_json(),                   # a.py audited fine
            OpenAIError(401, "bad key"),       # b.py fails auth
            # c.py never attempted
        ])
        outcome = _run(run_audit(
            project_id=pid,
            file_artifacts=self._make_file_artifacts("a.py", "b.py", "c.py"),
            client=client, store=store,
        ))
        self.assertEqual(outcome.audited_files, ["a.py"])
        self.assertEqual(len(outcome.failed_files), 1)
        self.assertEqual(outcome.failed_files[0][0], "b.py")
        # Two calls total: a.py (success) + b.py (401). c.py never tried.
        self.assertEqual(len(client.calls), 2)


# ---------------------------------------------------------------------------
# Handler-level tests through the dispatcher.
# ---------------------------------------------------------------------------

class TestHandlerAuditNoKey(unittest.TestCase):
    def test_missing_openai_writes_skip_record(self):
        async def go():
            store = InMemoryLedgerStore()
            pid = store.create_project("nokey", "x")
            # Pre-seed one file so the handler has something to look at.
            store.write_entry(
                project_id=pid, tier=Tier.GENERATION,
                artifact_kind=ArtifactKind.FILE,
                artifact_key=f"file:{pid}:a.py",
                body="print(1)\n",
                rationale="seeded by test",
                author="test",
            )
            ctx = HandlerContext(store=store, openai=None)
            await dispatch(make_job("audit_project", project_id=pid), ctx)
            return pid, store
        pid, store = _run(go())
        # Without an OpenAI client, handler writes skipped record and
        # nothing else.
        self.assertIsNotNone(
            store.current_entry(pid, f"ref:{pid}:audit:skipped"),
        )


class TestBuildAuditChaining(unittest.TestCase):
    def test_successful_build_enqueues_audit(self):
        """After a successful build, an audit_project job should be
        enqueued on ctx.queue. We don't run the audit here — just verify
        the chain handoff happens."""
        from anthropic_client import CompletionResult as AntResult
        from build_pipeline import _parse_spec  # for sanity

        class FakeAnt:
            def __init__(self, responses): self._r = list(responses); self.calls = []
            async def complete(self, prompt, *, system=None, model=None,
                               max_tokens=4096, temperature=0.2):
                self.calls.append(prompt)
                nxt = self._r.pop(0)
                if isinstance(nxt, AntResult):
                    return nxt
                return AntResult(
                    text=nxt, model="fake", stop_reason="end_turn",
                    input_tokens=100, output_tokens=200,
                )
            async def aclose(self): pass

        async def go():
            store = InMemoryLedgerStore()
            pid = store.create_project("chain", "build-then-audit")
            ant = FakeAnt([
                json.dumps({
                    "summary": "x",
                    "files": [
                        {"path": "a.py", "purpose": "p", "language": "python",
                         "imports": [], "size_hint": "small"},
                    ],
                }),
                "print(1)\n",
            ])
            queue = MemoryJobQueue()
            ctx = HandlerContext(store=store, anthropic=ant, queue=queue)
            await dispatch(make_job(
                "build_project", project_id=pid, prompt="build",
            ), ctx)
            # The chained audit job should be sitting on the queue.
            queued = await queue.dequeue(timeout_seconds=0.1)
            return queued

        queued = _run(go())
        self.assertIsNotNone(queued)
        self.assertEqual(queued["kind"], "audit_project")


# Imports for the test classes above that need them.
from ledger import ArtifactKind, Tier  # noqa: E402


if __name__ == "__main__":
    unittest.main()
