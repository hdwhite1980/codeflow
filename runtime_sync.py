"""
codeflow.runtime_sync
=====================

Connectors that project external system state into the ledger as nodes and
edges, plus the ChangeImpactAnalyzer that produces "if this changes, here's
what else has to change" reports.

Design pattern: webhook + reconciliation
----------------------------------------
Every external system gets two surfaces:

  1. A webhook receiver (`*Connector.on_webhook(payload)`) for low-latency
     updates. The receiver writes ledger entries immediately so the graph
     reflects user-facing reality fast.
  2. A reconciliation worker (`*Connector.reconcile()`) that polls the
     external system's API for full current state and corrects drift. This
     runs on a schedule (every 5 min for active projects, hourly idle) and
     fixes whatever the webhooks dropped.

Webhooks alone are insufficient. They fail silently in network blips, arrive
out of order under load, and providers will deprecate event types without
warning. Reconciliation is the price of "always knows current state".

This module intentionally does NOT hit real APIs in tests — every connector
takes an injectable client. That lets us prove the projection logic without
network dependencies or paid API quotas.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional, Protocol

from ledger import ArtifactKind, EdgeKind, Tier
from symbol_extractor import analyze_file


# ---------------------------------------------------------------------------
# Client protocols — each connector takes one of these. Production passes the
# real SDK (supabase-py, gql, requests-against-Railway), tests pass a fake.
# ---------------------------------------------------------------------------

class SupabaseLikeClient(Protocol):
    def list_tables(self) -> list[dict]: ...
    def list_columns(self, table: str) -> list[dict]: ...
    def list_indexes(self, table: str) -> list[dict]: ...


class RailwayLikeClient(Protocol):
    def list_services(self) -> list[dict]: ...
    def list_deployments(self, service_id: str) -> list[dict]: ...
    def list_env_vars(self, service_id: str) -> list[dict]: ...


class GithubLikeClient(Protocol):
    def list_commits(self, repo: str, branch: str, since: Optional[str]) -> list[dict]: ...
    def get_file_at(self, repo: str, sha: str, path: str) -> Optional[str]: ...
    def list_changed_files(self, repo: str, sha: str) -> list[str]: ...


# ---------------------------------------------------------------------------
# Database connector
# ---------------------------------------------------------------------------

@dataclass
class DatabaseConnector:
    """
    Projects DB schema into the ledger. Each table becomes a graph node;
    each column becomes a graph node connected to its table; foreign keys
    become fk_references edges between columns.

    The 'reads_column' / 'writes_column' edges from app-code symbols to
    columns are emitted by the symbol extractor, not here — this connector
    owns the database side of the graph, the extractor owns the code side.
    """
    store: Any                 # LedgerStore or InMemoryLedgerStore
    project_id: str
    db_artifact_key: str       # 'db:supabase_main'
    client: SupabaseLikeClient

    def reconcile(self) -> dict[str, int]:
        """Pull full schema state and project it into the ledger. Returns a
        summary dict: {tables: n, columns: n, indexes: n, drift_fixed: n}."""
        tables = self.client.list_tables()

        # Ensure the schema-level node exists.
        self.store.write_entry(
            project_id=self.project_id,
            tier=Tier.SPEC,                       # schema is part of spec surface
            artifact_kind=ArtifactKind("db_schema"),
            artifact_key=self.db_artifact_key,
            body={"tables": [t["name"] for t in tables]},
            rationale="Schema state from connector reconcile",
            author=f"db_connector:{self.db_artifact_key}",
        )

        col_count = 0
        idx_count = 0
        for t in tables:
            table_key = f"table:{self.db_artifact_key}:{t['name']}"
            self.store.write_entry(
                project_id=self.project_id,
                tier=Tier.SPEC,
                artifact_kind=ArtifactKind("db_table"),
                artifact_key=table_key,
                body={"name": t["name"], "columns": t.get("column_count", 0)},
                rationale=f"Table from {self.db_artifact_key}",
                author=f"db_connector:{self.db_artifact_key}",
                explicit_edges=[(EdgeKind("has_table"), self.db_artifact_key)],
            )
            for col in self.client.list_columns(t["name"]):
                col_key = f"column:{self.db_artifact_key}:{t['name']}.{col['name']}"
                edges = [(EdgeKind("has_column"), table_key)]
                # If this column has a foreign key, emit an fk edge.
                if col.get("references"):
                    ref_table, ref_col = col["references"].split(".")
                    ref_key = f"column:{self.db_artifact_key}:{ref_table}.{ref_col}"
                    edges.append((EdgeKind("fk_references"), ref_key))
                self.store.write_entry(
                    project_id=self.project_id,
                    tier=Tier.SPEC,
                    artifact_kind=ArtifactKind("db_column"),
                    artifact_key=col_key,
                    body={
                        "name": col["name"],
                        "type": col.get("type", "unknown"),
                        "nullable": col.get("nullable", True),
                    },
                    rationale=f"Column on {t['name']}",
                    author=f"db_connector:{self.db_artifact_key}",
                    explicit_edges=edges,
                )
                col_count += 1
            for idx in self.client.list_indexes(t["name"]):
                idx_key = f"index:{self.db_artifact_key}:{t['name']}.{idx['name']}"
                self.store.write_entry(
                    project_id=self.project_id,
                    tier=Tier.SPEC,
                    artifact_kind=ArtifactKind("db_index"),
                    artifact_key=idx_key,
                    body={"name": idx["name"], "columns": idx.get("columns", [])},
                    rationale=f"Index on {t['name']}",
                    author=f"db_connector:{self.db_artifact_key}",
                    explicit_edges=[(EdgeKind("has_index"), table_key)],
                )
                idx_count += 1

        return {
            "tables": len(tables),
            "columns": col_count,
            "indexes": idx_count,
        }

    def known_columns(self) -> dict[str, set[str]]:
        """Return {table_name: {col_name, ...}} for the symbol extractor's
        column-reference miner. We read this from the graph rather than
        re-polling the DB so all consumers see consistent state."""
        out: dict[str, set[str]] = {}
        # All current db_column entries for this project.
        cols = self.store.all_current(
            self.project_id, ArtifactKind("db_column")
        )
        # The artifact_key shape is 'column:<db_artifact_key>:<table>.<column>',
        # where db_artifact_key itself may contain colons (e.g. 'db:supabase_main').
        # We strip the known prefix to isolate the table.column suffix.
        prefix = f"column:{self.db_artifact_key}:"
        for entry in cols:
            if not entry.artifact_key.startswith(prefix):
                continue
            suffix = entry.artifact_key[len(prefix):]   # 'users.email'
            if "." not in suffix:
                continue
            table, col = suffix.split(".", 1)
            out.setdefault(table, set()).add(col)
        return out


# ---------------------------------------------------------------------------
# GitHub connector
# ---------------------------------------------------------------------------

@dataclass
class GitConnector:
    """
    Projects git history into the ledger and runs symbol extraction on the
    files that landed in each commit. Symbols become nodes; the file ->
    symbol edge is `contains_symbol`; symbol -> column edges come from the
    extractor.
    """
    store: Any
    project_id: str
    repo: str                  # 'orgname/reponame'
    branch: str                # 'main'
    client: GithubLikeClient
    db_connector: DatabaseConnector  # needed for known_columns lookup

    def ingest_commit(self, commit: dict) -> dict[str, Any]:
        """Project a single commit + its changed files into the graph."""
        sha = commit["sha"]
        commit_key = f"commit:{self.repo}:{sha}"

        self.store.write_entry(
            project_id=self.project_id,
            tier=Tier.GENERATION,    # code lands via generation/patch
            artifact_kind=ArtifactKind("git_commit"),
            artifact_key=commit_key,
            body={
                "sha": sha,
                "message": commit.get("message", ""),
                "author": commit.get("author", "unknown"),
            },
            rationale=f"Commit on {self.branch}",
            author=f"git_connector:{self.repo}",
        )

        known_cols = self.db_connector.known_columns()
        changed = self.client.list_changed_files(self.repo, sha)
        symbols_extracted = 0
        col_refs_emitted = 0

        for path in changed:
            source = self.client.get_file_at(self.repo, sha, path)
            if source is None:
                continue
            file_key = f"file:{path}"
            # Update the file entry. Edge to commit lets us answer
            # "what changed in this deploy".
            self.store.write_entry(
                project_id=self.project_id,
                tier=Tier.GENERATION,
                artifact_kind=ArtifactKind.FILE,
                artifact_key=file_key,
                body=source,
                rationale=f"File state at {sha[:8]}",
                author=f"git_connector:{self.repo}",
                explicit_edges=[(EdgeKind("defined_in_commit"), commit_key)],
            )
            # Symbol extraction.
            analysis = analyze_file(path, source, known_cols)
            for sym in analysis.symbols:
                sym_key = sym.artifact_key
                self.store.write_entry(
                    project_id=self.project_id,
                    tier=Tier.GENERATION,
                    artifact_kind=ArtifactKind("function_symbol"),
                    artifact_key=sym_key,
                    body={
                        "name": sym.name, "kind": sym.kind,
                        "line_start": sym.line_start,
                        "line_end": sym.line_end,
                        "parent": sym.parent,
                    },
                    rationale=f"{sym.kind} in {path}",
                    author=f"symbol_extractor:{analysis.language}",
                    explicit_edges=[
                        (EdgeKind("contains_symbol"), file_key),
                    ],
                )
                symbols_extracted += 1
                # Column reference edges. We write the edge FROM the function
                # symbol TO the column, so neighbors(symbol, direction=out)
                # returns the columns it touches. The ledger entry that
                # declares the edge is the function_symbol entry itself —
                # we re-write it with explicit edges added for each column.
                #
                # The contains_symbol edge points FROM symbol TO file. This
                # means impact_set walking incoming edges from a column reaches
                # the symbols, and walking from a symbol reaches its file —
                # which is what we want for ripple analysis.
                col_edges: list[tuple[EdgeKind, str]] = [
                    (EdgeKind("contains_symbol"), file_key),
                ]
                for ref in analysis.refs.get(sym_key, []):
                    col_key = f"column:{self.db_connector.db_artifact_key}:{ref.table}.{ref.column}"
                    col_edges.append((EdgeKind(ref.edge_kind()), col_key))
                    col_refs_emitted += 1
                # Re-write the symbol with the full edge list. The previous
                # write_entry call above created the symbol; this supersedes
                # it with the edges attached. (In a refactor we'd build the
                # edge list before the first write; doing it here keeps the
                # diff minimal.)
                if len(col_edges) > 1:
                    self.store.write_entry(
                        project_id=self.project_id,
                        tier=Tier.GENERATION,
                        artifact_kind=ArtifactKind("function_symbol"),
                        artifact_key=sym_key,
                        body={
                            "name": sym.name, "kind": sym.kind,
                            "line_start": sym.line_start,
                            "line_end": sym.line_end,
                            "parent": sym.parent,
                        },
                        rationale=(
                            f"{sym.kind} in {path} with "
                            f"{len(col_edges) - 1} db column refs"
                        ),
                        author=f"symbol_extractor:{analysis.language}",
                        explicit_edges=col_edges,
                    )

        return {
            "commit": sha,
            "files_changed": len(changed),
            "symbols": symbols_extracted,
            "column_refs": col_refs_emitted,
        }

    def on_webhook(self, payload: dict) -> None:
        """Process a GitHub push webhook. We re-fetch commit details rather
        than trust the payload — defensive, since webhooks can be replayed
        or forged."""
        for commit_summary in payload.get("commits", []):
            sha = commit_summary["id"]
            # In production, fetch full commit via client; tests pass it pre-shaped.
            commits = self.client.list_commits(self.repo, self.branch, sha)
            for c in commits:
                if c["sha"] == sha:
                    self.ingest_commit(c)
                    break


# ---------------------------------------------------------------------------
# Railway connector
# ---------------------------------------------------------------------------

@dataclass
class RailwayConnector:
    """
    Projects services, deploys, and env vars into the ledger. A deploy
    links a commit to a service; env vars become nodes so the audit layer
    can flag "this code reads SECRET_KEY but no service binds it".
    """
    store: Any
    project_id: str
    client: RailwayLikeClient

    def reconcile(self) -> dict[str, int]:
        services = self.client.list_services()
        deploy_count = 0
        env_count = 0
        for svc in services:
            svc_key = f"service:{svc['id']}"
            self.store.write_entry(
                project_id=self.project_id,
                tier=Tier.SPEC,
                artifact_kind=ArtifactKind("service"),
                artifact_key=svc_key,
                body={"name": svc["name"], "id": svc["id"]},
                rationale="Service from Railway reconcile",
                author="railway_connector",
            )
            for env in self.client.list_env_vars(svc["id"]):
                env_key = f"env:{svc['id']}:{env['name']}"
                self.store.write_entry(
                    project_id=self.project_id,
                    tier=Tier.SPEC,
                    artifact_kind=ArtifactKind.ENV_VAR,
                    artifact_key=env_key,
                    body={
                        "name": env["name"],
                        # NEVER store the value — only that the binding exists.
                        "has_value": bool(env.get("value")),
                    },
                    rationale=f"Env var on {svc['name']}",
                    author="railway_connector",
                    explicit_edges=[(EdgeKind("binds_env_var"), svc_key)],
                )
                env_count += 1
            for dep in self.client.list_deployments(svc["id"]):
                dep_key = f"deploy:{dep['id']}"
                edges = [(EdgeKind("runs_service"), svc_key)]
                if dep.get("commit_sha"):
                    edges.append((
                        EdgeKind("deployed_in"),
                        f"commit:{dep['repo']}:{dep['commit_sha']}",
                    ))
                self.store.write_entry(
                    project_id=self.project_id,
                    tier=Tier.GENERATION,
                    artifact_kind=ArtifactKind("deploy"),
                    artifact_key=dep_key,
                    body={
                        "id": dep["id"],
                        "status": dep.get("status", "unknown"),
                        "created_at": dep.get("created_at"),
                    },
                    rationale=f"Deploy of {svc['name']}",
                    author="railway_connector",
                    explicit_edges=edges,
                )
                deploy_count += 1
        return {
            "services": len(services),
            "deploys": deploy_count,
            "env_vars": env_count,
        }


# ---------------------------------------------------------------------------
# ChangeImpactAnalyzer — the user-facing 'if this changes, that breaks' API
# ---------------------------------------------------------------------------

@dataclass
class ImpactReport:
    """Human-readable report for a planned change."""
    target_artifact: str
    description: str
    affected_files: list[str]
    affected_symbols: list[str]
    affected_db_objects: list[str]
    affected_deploys: list[str]
    suggested_db_changes: list[str]
    severity: str               # 'low' | 'medium' | 'high' | 'breaking'
    notes: list[str]

    def to_dict(self) -> dict:
        return {
            "target": self.target_artifact,
            "description": self.description,
            "affected": {
                "files": self.affected_files,
                "symbols": self.affected_symbols,
                "db_objects": self.affected_db_objects,
                "deploys": self.affected_deploys,
            },
            "suggested_db_changes": self.suggested_db_changes,
            "severity": self.severity,
            "notes": self.notes,
        }


class ChangeImpactAnalyzer:
    """
    The "if you change X, here's what else breaks" answer. Walks the graph
    from a target node outward via INCOMING edges to enumerate everything
    that depends on it. Categorizes the impact by artifact kind so a UI can
    show files, symbols, db objects, and deploys as separate columns.
    """

    def __init__(self, store: Any, project_id: str) -> None:
        self.store = store
        self.project_id = project_id

    def analyze_change(
        self, target_key: str, description: str,
    ) -> ImpactReport:
        impact = self.store.impact_set(self.project_id, target_key)

        # Expand: for every impacted function_symbol, the file that contains
        # it is also impacted. The contains_symbol edge points symbol -> file,
        # so we walk OUTGOING edges from each symbol to find its file.
        expanded = set(impact)
        for key in list(impact):
            if key.startswith("func:"):
                file_edges = self.store.neighbors(
                    self.project_id, key, direction="out",
                    edge_kinds=[EdgeKind("contains_symbol")],
                )
                for e in file_edges:
                    expanded.add(e.to_artifact_key)
        # From files: propagate to commits (defined_in_commit) and any other
        # incoming dependencies.
        for file_key in [k for k in expanded if k.startswith("file:")]:
            commit_edges = self.store.neighbors(
                self.project_id, file_key, direction="out",
                edge_kinds=[EdgeKind("defined_in_commit")],
            )
            for e in commit_edges:
                expanded.add(e.to_artifact_key)
            file_ripple = self.store.impact_set(self.project_id, file_key)
            expanded.update(file_ripple)
        # From commits: deploys point AT commits via deployed_in, so walk
        # INCOMING edges from each commit to find every deploy of that commit.
        for commit_key in [k for k in expanded if k.startswith("commit:")]:
            deploy_edges = self.store.neighbors(
                self.project_id, commit_key, direction="in",
                edge_kinds=[EdgeKind("deployed_in")],
            )
            for e in deploy_edges:
                expanded.add(e.from_artifact_key)

        impact = expanded

        files: list[str] = []
        symbols: list[str] = []
        db_objects: list[str] = []
        deploys: list[str] = []
        notes: list[str] = []

        for key in sorted(impact):
            prefix = key.split(":", 1)[0]
            if prefix == "file":
                files.append(key)
            elif prefix == "func":
                symbols.append(key)
            elif prefix in {"table", "column", "index", "db"}:
                db_objects.append(key)
            elif prefix == "deploy":
                deploys.append(key)

        # Suggest DB changes when the target is a db_column and code
        # writes/reads it. The most common "needs DB update" case:
        # - Renaming/removing a column: every writes_column edge from app
        #   code is now stranded; we suggest the migration.
        # - Adding a feature: a manifest item not satisfied by any function
        #   that writes the needed column.
        suggested_db_changes: list[str] = []
        if target_key.startswith("column:"):
            # The change is to a column. Every symbol that reads/writes it
            # needs review. If any writes_column edge exists, dropping the
            # column would break those writes — that's a breaking change.
            in_edges = self.store.neighbors(
                self.project_id, target_key, direction="in",
                edge_kinds=[EdgeKind("reads_column"), EdgeKind("writes_column")],
            )
            writers = [e for e in in_edges
                       if e.edge_kind == EdgeKind("writes_column")]
            readers = [e for e in in_edges
                       if e.edge_kind == EdgeKind("reads_column")]
            if writers:
                notes.append(
                    f"{len(writers)} code site(s) WRITE this column. "
                    "Dropping it is a breaking change."
                )
            if readers:
                notes.append(
                    f"{len(readers)} code site(s) READ this column. "
                    "Renaming requires a coordinated migration."
                )
            suggested_db_changes.append(
                f"Plan a migration that updates {target_key} before deploying "
                "the new app code, or use a two-phase rollout."
            )

        # Severity heuristic.
        if any(k.startswith("deploy:") for k in impact):
            severity = "breaking"   # a currently-deployed service is affected
        elif len(files) >= 5 or len(symbols) >= 10:
            severity = "high"
        elif files or symbols or db_objects:
            severity = "medium"
        else:
            severity = "low"

        return ImpactReport(
            target_artifact=target_key,
            description=description,
            affected_files=files,
            affected_symbols=symbols,
            affected_db_objects=db_objects,
            affected_deploys=deploys,
            suggested_db_changes=suggested_db_changes,
            severity=severity,
            notes=notes,
        )

    def analyze_proposed_db_change(
        self, db_artifact_key: str, change_type: str,
        target_table: str, target_column: Optional[str] = None,
    ) -> ImpactReport:
        """
        Specialized entry point for proposed DB migrations. change_type:
        'drop_column', 'rename_column', 'add_column', 'drop_table',
        'add_table'. The analyzer constructs the right target_key and
        suggests the migration sequencing.
        """
        if change_type == "drop_column" and target_column:
            key = f"column:{db_artifact_key}:{target_table}.{target_column}"
            return self.analyze_change(
                key, f"Drop column {target_table}.{target_column}"
            )
        if change_type == "rename_column" and target_column:
            key = f"column:{db_artifact_key}:{target_table}.{target_column}"
            report = self.analyze_change(
                key, f"Rename column {target_table}.{target_column}"
            )
            report.suggested_db_changes.append(
                "Use expand/contract pattern: add new column, dual-write, "
                "migrate readers, drop old column."
            )
            return report
        if change_type == "drop_table":
            key = f"table:{db_artifact_key}:{target_table}"
            return self.analyze_change(key, f"Drop table {target_table}")
        if change_type == "add_column":
            # Adding is low-risk for existing code; check whether any
            # manifest item is gated on this new column.
            return ImpactReport(
                target_artifact=f"column:{db_artifact_key}:{target_table}.{target_column}",
                description=f"Add column {target_table}.{target_column}",
                affected_files=[], affected_symbols=[],
                affected_db_objects=[f"table:{db_artifact_key}:{target_table}"],
                affected_deploys=[],
                suggested_db_changes=[
                    "Additive change. Apply migration before deploying code "
                    "that uses the new column. Make the column nullable or "
                    "give it a default so existing rows remain valid."
                ],
                severity="low",
                notes=["Forward-compatible change."],
            )
        if change_type == "add_table":
            return ImpactReport(
                target_artifact=f"table:{db_artifact_key}:{target_table}",
                description=f"Add table {target_table}",
                affected_files=[], affected_symbols=[],
                affected_db_objects=[f"db:{db_artifact_key}"],
                affected_deploys=[],
                suggested_db_changes=[
                    "Additive change. Apply migration; no existing code "
                    "should be affected."
                ],
                severity="low",
                notes=["Forward-compatible change."],
            )
        raise ValueError(f"unknown change_type: {change_type}")
