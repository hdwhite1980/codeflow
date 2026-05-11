"""
test_usage_recording
====================

Tests for:
  * pricing.lookup_price / compute_costs (model name normalization)
  * usage_recorder.InMemoryUsageRecorder (record + list)
  * usage_recorder.summarize (rollups by stage and provider)
  * end-to-end: run_build with a recorder produces correct per-call rows
  * end-to-end: run_audit with a recorder produces correct per-call rows

We don't test PostgresUsageRecorder here — it's a thin SQL wrapper and
the integration test would require a real database. The protocol is
shared with InMemoryUsageRecorder so coverage transfers.
"""

from __future__ import annotations

import asyncio
import json
import unittest

from anthropic_client import CompletionResult as AntResult
from audit_pipeline import run_audit
from build_pipeline import run_build
from ledger_memory import InMemoryLedgerStore
from openai_client import CompletionResult as OAIResult
from pricing import PRICING_PER_MTOK, compute_costs, lookup_price
from usage_recorder import (
    InMemoryUsageRecorder, ProjectUsageSummary, summarize,
)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Pricing tests.
# ---------------------------------------------------------------------------

class TestPricing(unittest.TestCase):
    def test_known_model_lookup(self):
        # claude-sonnet-4-6 should be priced.
        price = lookup_price("claude-sonnet-4-6")
        self.assertIsNotNone(price)
        in_, out = price
        self.assertGreater(in_, 0)
        self.assertGreater(out, in_)  # output always costs more than input

    def test_unknown_model_returns_none(self):
        self.assertIsNone(lookup_price("totally-made-up-2099"))

    def test_date_suffix_stripped_for_lookup(self):
        # "gpt-5.4-mini-2026-03-17" should resolve to "gpt-5.4-mini"
        # via the date-suffix normalizer.
        price = lookup_price("gpt-5.4-mini-2026-03-17")
        self.assertIsNotNone(price)
        self.assertEqual(price, lookup_price("gpt-5.4-mini"))

    def test_compute_costs_arithmetic(self):
        # Sonnet 4.6: $3/M input, $15/M output. 1000 in / 500 out:
        #   input = 1000/1e6 * 3 = 0.003
        #   output = 500/1e6 * 15 = 0.0075
        in_cost, out_cost = compute_costs("claude-sonnet-4-6", 1000, 500)
        self.assertAlmostEqual(in_cost, 0.003, places=6)
        self.assertAlmostEqual(out_cost, 0.0075, places=6)

    def test_compute_costs_unknown_model_returns_zero(self):
        in_cost, out_cost = compute_costs("never-heard-of-it", 1000, 1000)
        self.assertEqual(in_cost, 0.0)
        self.assertEqual(out_cost, 0.0)

    def test_compute_costs_zero_tokens(self):
        # Zero usage means zero cost, even for known models.
        in_cost, out_cost = compute_costs("claude-sonnet-4-6", 0, 0)
        self.assertEqual(in_cost, 0.0)
        self.assertEqual(out_cost, 0.0)


# ---------------------------------------------------------------------------
# InMemoryUsageRecorder tests.
# ---------------------------------------------------------------------------

class TestInMemoryRecorder(unittest.TestCase):
    def test_records_appear_in_list(self):
        rec = InMemoryUsageRecorder()
        rec.record(
            project_id="p1", provider="anthropic",
            model="claude-sonnet-4-6", stage="spec",
            subject=None, input_tokens=100, output_tokens=50,
        )
        rows = rec.list_for_project("p1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].provider, "anthropic")
        self.assertEqual(rows[0].stage, "spec")
        self.assertEqual(rows[0].input_tokens, 100)
        self.assertGreater(rows[0].input_cost_usd, 0)

    def test_list_filters_by_project(self):
        rec = InMemoryUsageRecorder()
        rec.record(project_id="p1", provider="x", model="m", stage="s",
                   subject=None, input_tokens=1, output_tokens=1)
        rec.record(project_id="p2", provider="x", model="m", stage="s",
                   subject=None, input_tokens=2, output_tokens=2)
        self.assertEqual(len(rec.list_for_project("p1")), 1)
        self.assertEqual(len(rec.list_for_project("p2")), 1)

    def test_total_cost_property(self):
        rec = InMemoryUsageRecorder()
        rec.record(project_id="p1", provider="anthropic",
                   model="claude-sonnet-4-6", stage="spec",
                   subject=None, input_tokens=1000, output_tokens=500)
        row = rec.list_for_project("p1")[0]
        # Should be input + output as computed by pricing.
        self.assertAlmostEqual(row.total_cost_usd, 0.003 + 0.0075, places=6)


# ---------------------------------------------------------------------------
# summarize() tests.
# ---------------------------------------------------------------------------

class TestSummarize(unittest.TestCase):
    def test_empty_summary(self):
        s = summarize("p1", [])
        self.assertEqual(s.total_tokens, 0)
        self.assertEqual(s.total_cost_usd, 0.0)
        self.assertEqual(s.by_stage, {})
        self.assertEqual(s.by_provider, {})

    def test_rollups_by_stage_and_provider(self):
        rec = InMemoryUsageRecorder()
        # One spec call + two file calls + two audit calls.
        rec.record(project_id="p", provider="anthropic",
                   model="claude-sonnet-4-6", stage="spec",
                   subject=None, input_tokens=1000, output_tokens=500)
        for path in ["a.py", "b.py"]:
            rec.record(project_id="p", provider="anthropic",
                       model="claude-sonnet-4-6", stage="file",
                       subject=path, input_tokens=2000, output_tokens=1000)
        for path in ["a.py", "b.py"]:
            rec.record(project_id="p", provider="openai",
                       model="gpt-5.4-mini", stage="audit",
                       subject=path, input_tokens=800, output_tokens=200)
        s = summarize("p", rec.list_for_project("p"))

        # Counts.
        self.assertEqual(len(s.rows), 5)
        # Stage rollup.
        self.assertEqual(s.by_stage["spec"].call_count, 1)
        self.assertEqual(s.by_stage["file"].call_count, 2)
        self.assertEqual(s.by_stage["audit"].call_count, 2)
        self.assertEqual(s.by_stage["file"].input_tokens, 4000)
        self.assertEqual(s.by_stage["audit"].output_tokens, 400)
        # Provider rollup.
        self.assertEqual(s.by_provider["anthropic"].call_count, 3)
        self.assertEqual(s.by_provider["openai"].call_count, 2)

    def test_to_dict_shape(self):
        rec = InMemoryUsageRecorder()
        rec.record(project_id="p", provider="anthropic",
                   model="claude-sonnet-4-6", stage="spec",
                   subject=None, input_tokens=100, output_tokens=50)
        s = summarize("p", rec.list_for_project("p"))
        d = s.to_dict()
        self.assertIn("project_id", d)
        self.assertIn("total_cost_usd", d)
        self.assertIn("by_stage", d)
        self.assertIn("by_provider", d)
        self.assertIn("rows", d)
        # Structure of by_stage list.
        stage_row = d["by_stage"][0]
        self.assertIn("stage", stage_row)
        self.assertIn("call_count", stage_row)
        self.assertIn("total_cost_usd", stage_row)


# ---------------------------------------------------------------------------
# End-to-end: build pipeline records every API call.
# ---------------------------------------------------------------------------

class FakeAnthropic:
    """Returns scripted responses with controllable token counts so we
    can assert on exact recorded values."""
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
    async def complete(self, prompt, *, system=None, model=None,
                       max_tokens=4096, temperature=0.2):
        self.calls.append({"prompt": prompt})
        nxt = self._responses.pop(0)
        return nxt
    async def aclose(self): pass


def ant_result(text, *, in_tok, out_tok, model="claude-sonnet-4-6"):
    return AntResult(text=text, model=model, stop_reason="end_turn",
                     input_tokens=in_tok, output_tokens=out_tok)


def spec_response(*files):
    return json.dumps({
        "summary": "x",
        "files": [
            {"path": f, "purpose": "p", "language": "python",
             "imports": [], "size_hint": "small"}
            for f in files
        ],
    })


class TestBuildRecordsUsage(unittest.TestCase):
    def test_spec_and_file_calls_each_recorded(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("rec", "record this")
        rec = InMemoryUsageRecorder()
        client = FakeAnthropic([
            ant_result(spec_response("a.py", "b.py"), in_tok=500, out_tok=200),
            ant_result("print('a')\n", in_tok=1000, out_tok=400),
            ant_result("print('b')\n", in_tok=1100, out_tok=350),
        ])
        _run(run_build(
            project_id=pid, prompt="x",
            client=client, store=store, recorder=rec,
        ))
        rows = rec.list_for_project(pid)
        # 1 spec + 2 file = 3 rows
        self.assertEqual(len(rows), 3)
        # Spec row first.
        self.assertEqual(rows[0].stage, "spec")
        self.assertIsNone(rows[0].subject)
        self.assertEqual(rows[0].input_tokens, 500)
        # File rows have subjects.
        file_rows = [r for r in rows if r.stage == "file"]
        self.assertEqual(len(file_rows), 2)
        self.assertEqual({r.subject for r in file_rows}, {"a.py", "b.py"})
        # All provider = anthropic.
        self.assertTrue(all(r.provider == "anthropic" for r in rows))

    def test_failed_spec_still_records_call(self):
        """Retries cost real money. Each attempt should be recorded even
        if the spec ultimately fails."""
        store = InMemoryLedgerStore()
        pid = store.create_project("fail", "x")
        rec = InMemoryUsageRecorder()
        client = FakeAnthropic([
            ant_result("garbage", in_tok=400, out_tok=100),
            ant_result("still garbage", in_tok=420, out_tok=110),
        ])
        outcome = _run(run_build(
            project_id=pid, prompt="x",
            client=client, store=store, recorder=rec,
        ))
        self.assertFalse(outcome.succeeded)
        rows = rec.list_for_project(pid)
        # Both spec attempts recorded.
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.stage == "spec" for r in rows))


# ---------------------------------------------------------------------------
# End-to-end: audit pipeline records every API call.
# ---------------------------------------------------------------------------

class FakeOpenAI:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []
    async def complete(self, prompt, *, system=None, model=None,
                       max_tokens=2048, temperature=0.2, json_mode=False):
        self.calls.append({"prompt": prompt})
        nxt = self._responses.pop(0)
        return nxt
    async def aclose(self): pass


def oai_result(text, *, in_tok, out_tok, model="gpt-5.4-mini"):
    return OAIResult(text=text, model=model, finish_reason="stop",
                     input_tokens=in_tok, output_tokens=out_tok)


class TestAuditRecordsUsage(unittest.TestCase):
    def test_each_audited_file_produces_one_usage_row(self):
        store = InMemoryLedgerStore()
        pid = store.create_project("aud", "audit usage test")
        rec = InMemoryUsageRecorder()
        client = FakeOpenAI([
            oai_result(json.dumps({"findings": []}), in_tok=600, out_tok=20),
            oai_result(json.dumps({"findings": []}), in_tok=700, out_tok=30),
        ])
        _run(run_audit(
            project_id=pid,
            file_artifacts=[
                {"path": "a.py", "content": "x", "purpose": "p", "language": "py"},
                {"path": "b.py", "content": "x", "purpose": "p", "language": "py"},
            ],
            client=client, store=store, recorder=rec,
        ))
        rows = rec.list_for_project(pid)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r.stage == "audit" for r in rows))
        self.assertTrue(all(r.provider == "openai" for r in rows))
        self.assertEqual({r.subject for r in rows}, {"a.py", "b.py"})


if __name__ == "__main__":
    unittest.main()
