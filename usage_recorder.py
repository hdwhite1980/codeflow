"""
codeflow.usage_recorder
=======================

Records one row in the `token_usage` table per AI API call. Used by both
the build pipeline (Anthropic spec + file calls) and the audit pipeline
(OpenAI per-file audit calls).

Why a recorder class
--------------------
We want the call sites in the pipelines to be one line: "after I got a
CompletionResult, record it." All the database plumbing, cost lookup,
and error handling lives here. If recording fails for some reason
(database hiccup, schema mismatch), we log and continue — we never let
a recording failure tank a build, because the build itself succeeded
and the user already got their files.

Production vs test
------------------
`PostgresUsageRecorder` is the real implementation. `InMemoryUsageRecorder`
is for tests; same interface, no database. Both implement the
`UsageRecorder` protocol so the pipelines accept either without caring.

Provider-agnostic
-----------------
The recorder doesn't know about Anthropic or OpenAI specifically. It
takes generic parameters (provider, model, stage, tokens) and writes a
row. Adding a third or fourth provider later doesn't touch this file.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Iterable, Optional, Protocol

import psycopg
from psycopg.rows import dict_row

from pricing import compute_costs


# ---------------------------------------------------------------------------
# Read shape — what callers see when they query usage back.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class UsageRow:
    id: str
    project_id: str
    created_at: str           # ISO format
    provider: str
    model: str
    stage: str
    subject: Optional[str]
    input_tokens: int
    output_tokens: int
    input_cost_usd: float
    output_cost_usd: float

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_cost_usd(self) -> float:
        return self.input_cost_usd + self.output_cost_usd

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "created_at": self.created_at,
            "provider": self.provider,
            "model": self.model,
            "stage": self.stage,
            "subject": self.subject,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_cost_usd": float(self.input_cost_usd),
            "output_cost_usd": float(self.output_cost_usd),
            "total_tokens": self.total_tokens,
            "total_cost_usd": float(self.total_cost_usd),
        }


# ---------------------------------------------------------------------------
# Protocol — what pipelines call.
# ---------------------------------------------------------------------------

class UsageRecorder(Protocol):
    def record(
        self,
        *,
        project_id: str,
        provider: str,
        model: str,
        stage: str,
        subject: Optional[str],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        ...

    def list_for_project(self, project_id: str) -> list[UsageRow]:
        ...


# ---------------------------------------------------------------------------
# Postgres implementation.
# ---------------------------------------------------------------------------

class PostgresUsageRecorder:
    """Production recorder. Opens a connection per call (no pool needed —
    one row insert is fast, and the build/audit pipeline is bursty rather
    than sustained-high-rate)."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    @contextmanager
    def _connect(self):
        with psycopg.connect(self._database_url, row_factory=dict_row) as conn:
            yield conn

    def record(
        self,
        *,
        project_id: str,
        provider: str,
        model: str,
        stage: str,
        subject: Optional[str],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        """Write one usage row. Failures are logged and swallowed."""
        try:
            input_cost, output_cost = compute_costs(model, input_tokens, output_tokens)
            with self._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO token_usage
                        (project_id, provider, model, stage, subject,
                         input_tokens, output_tokens,
                         input_cost_usd, output_cost_usd)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (project_id, provider, model, stage, subject,
                     input_tokens, output_tokens,
                     input_cost, output_cost),
                )
                conn.commit()
        except Exception as exc:
            # We intentionally don't re-raise. A failure to record usage
            # should never abort an in-progress build. We log loudly so
            # an operator notices.
            print(f"[usage] failed to record usage for project {project_id} "
                  f"stage={stage} model={model}: {exc!r}")

    def list_for_project(self, project_id: str) -> list[UsageRow]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, project_id, created_at, provider, model, stage,
                       subject, input_tokens, output_tokens,
                       input_cost_usd, output_cost_usd
                FROM token_usage
                WHERE project_id = %s
                ORDER BY created_at ASC
                """,
                (project_id,),
            )
            rows = cur.fetchall()
        return [
            UsageRow(
                id=str(r["id"]),
                project_id=str(r["project_id"]),
                created_at=r["created_at"].isoformat(),
                provider=r["provider"],
                model=r["model"],
                stage=r["stage"],
                subject=r["subject"],
                input_tokens=r["input_tokens"],
                output_tokens=r["output_tokens"],
                input_cost_usd=float(r["input_cost_usd"]),
                output_cost_usd=float(r["output_cost_usd"]),
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# In-memory implementation for tests and dev.
# ---------------------------------------------------------------------------

class InMemoryUsageRecorder:
    """Test recorder. Keeps a list of recorded calls in memory."""

    def __init__(self) -> None:
        self._rows: list[UsageRow] = []
        self._lock = threading.Lock()
        self._counter = 0

    def record(
        self,
        *,
        project_id: str,
        provider: str,
        model: str,
        stage: str,
        subject: Optional[str],
        input_tokens: int,
        output_tokens: int,
    ) -> None:
        input_cost, output_cost = compute_costs(model, input_tokens, output_tokens)
        with self._lock:
            self._counter += 1
            self._rows.append(UsageRow(
                id=f"mem-{self._counter}",
                project_id=project_id,
                created_at="1970-01-01T00:00:00+00:00",
                provider=provider,
                model=model,
                stage=stage,
                subject=subject,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                input_cost_usd=input_cost,
                output_cost_usd=output_cost,
            ))

    def list_for_project(self, project_id: str) -> list[UsageRow]:
        with self._lock:
            return [r for r in self._rows if r.project_id == project_id]


# ---------------------------------------------------------------------------
# Summary aggregator. Used by the API endpoint to roll up per-project data.
# ---------------------------------------------------------------------------

@dataclass
class StageSummary:
    stage: str
    call_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    input_cost_usd: float = 0.0
    output_cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def total_cost_usd(self) -> float:
        return self.input_cost_usd + self.output_cost_usd

    def to_dict(self) -> dict:
        return {
            "stage": self.stage,
            "call_count": self.call_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "input_cost_usd": self.input_cost_usd,
            "output_cost_usd": self.output_cost_usd,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
        }


@dataclass
class ProjectUsageSummary:
    project_id: str
    rows: list[UsageRow] = field(default_factory=list)
    by_stage: dict[str, StageSummary] = field(default_factory=dict)
    by_provider: dict[str, StageSummary] = field(default_factory=dict)

    @property
    def total_input_tokens(self) -> int:
        return sum(r.input_tokens for r in self.rows)

    @property
    def total_output_tokens(self) -> int:
        return sum(r.output_tokens for r in self.rows)

    @property
    def total_tokens(self) -> int:
        return self.total_input_tokens + self.total_output_tokens

    @property
    def total_cost_usd(self) -> float:
        return sum(r.total_cost_usd for r in self.rows)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "call_count": len(self.rows),
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_tokens,
            "total_cost_usd": self.total_cost_usd,
            "by_stage": [s.to_dict() for s in self.by_stage.values()],
            "by_provider": [s.to_dict() for s in self.by_provider.values()],
            "rows": [r.to_dict() for r in self.rows],
        }


def summarize(project_id: str, rows: Iterable[UsageRow]) -> ProjectUsageSummary:
    """Build a per-project summary with stage and provider rollups."""
    summary = ProjectUsageSummary(project_id=project_id, rows=list(rows))
    for r in summary.rows:
        stage = summary.by_stage.setdefault(r.stage, StageSummary(stage=r.stage))
        stage.call_count += 1
        stage.input_tokens += r.input_tokens
        stage.output_tokens += r.output_tokens
        stage.input_cost_usd += r.input_cost_usd
        stage.output_cost_usd += r.output_cost_usd

        prov = summary.by_provider.setdefault(
            r.provider, StageSummary(stage=r.provider),
        )
        prov.call_count += 1
        prov.input_tokens += r.input_tokens
        prov.output_tokens += r.output_tokens
        prov.input_cost_usd += r.input_cost_usd
        prov.output_cost_usd += r.output_cost_usd
    return summary
