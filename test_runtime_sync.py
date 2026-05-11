"""
test_runtime_sync.py
====================

Proves the whole pipeline works end-to-end with fake clients standing in for
Supabase, Railway, and GitHub. The fakes are deliberately small and
deterministic so the test is repeatable.

The scenario walks through:
  1. DB connector reconciles a small schema into the graph.
  2. Git connector ingests a commit; symbol extractor runs on real source.
  3. Column-reference edges land in the graph.
  4. We propose dropping a column and verify ChangeImpactAnalyzer surfaces
     every code site that would break, plus the right migration advice.
  5. We extend the graph with a Railway deploy and verify severity escalates.
"""

from __future__ import annotations

import json

from ledger import ArtifactKind, EdgeKind, Tier
from ledger_memory import InMemoryLedgerStore
from runtime_sync import (
    ChangeImpactAnalyzer, DatabaseConnector, GitConnector, RailwayConnector,
)


# ---------------------------------------------------------------------------
# Fake clients
# ---------------------------------------------------------------------------

class FakeSupabase:
    def __init__(self):
        self.tables = [
            {"name": "users", "column_count": 4},
            {"name": "boards", "column_count": 3},
        ]
        self.columns = {
            "users": [
                {"name": "id", "type": "uuid", "nullable": False},
                {"name": "email", "type": "text", "nullable": False},
                {"name": "legacy_username", "type": "text", "nullable": True},
                {"name": "created_at", "type": "timestamptz", "nullable": False},
            ],
            "boards": [
                {"name": "id", "type": "uuid", "nullable": False},
                {"name": "owner_id", "type": "uuid", "nullable": False,
                 "references": "users.id"},
                {"name": "title", "type": "text", "nullable": False},
            ],
        }
        self.indexes = {
            "users":  [{"name": "users_email_idx", "columns": ["email"]}],
            "boards": [{"name": "boards_owner_idx", "columns": ["owner_id"]}],
        }

    def list_tables(self): return self.tables
    def list_columns(self, table): return self.columns.get(table, [])
    def list_indexes(self, table): return self.indexes.get(table, [])


# Real source code for the symbol extractor to chew on. The TypeScript file
# reads users.legacy_username — the column we'll later try to drop.
USERS_TS = """
import { db } from './db';

export async function getUserProfile(id: string) {
  const result = await db.from('users').select('id, email, legacy_username').eq('id', id);
  return result.data?.[0];
}

export async function updateUserEmail(id: string, email: string) {
  await db.from('users').update({ email: email }).eq('id', id);
}

export class UserService {
  async findByLegacy(username: string) {
    const sql = `SELECT id, email FROM users WHERE legacy_username = $1`;
    return db.query(sql, [username]);
  }
}
""".lstrip()

BOARDS_TS = """
import { db } from './db';

export async function createBoard(ownerId: string, title: string) {
  return db.from('boards').insert({ owner_id: ownerId, title: title });
}
""".lstrip()


class FakeGithub:
    def __init__(self):
        self.commits = [{
            "sha": "abc123def456",
            "message": "initial: users + boards",
            "author": "claude",
        }]
        self.files = {
            "abc123def456": {
                "src/api/users.ts":  USERS_TS,
                "src/api/boards.ts": BOARDS_TS,
            },
        }

    def list_commits(self, repo, branch, since):
        return self.commits

    def get_file_at(self, repo, sha, path):
        return self.files.get(sha, {}).get(path)

    def list_changed_files(self, repo, sha):
        return list(self.files.get(sha, {}).keys())


class FakeRailway:
    def __init__(self, repo: str, commit_sha: str):
        self.services = [{"id": "svc-1", "name": "api"}]
        self.deployments = {"svc-1": [{
            "id": "dep-1", "status": "success",
            "created_at": "2026-05-11T10:00:00Z",
            "repo": repo, "commit_sha": commit_sha,
        }]}
        self.env_vars = {"svc-1": [
            {"name": "DATABASE_URL", "value": "postgres://..."},
            {"name": "JWT_SECRET",   "value": "redacted"},
        ]}

    def list_services(self): return self.services
    def list_deployments(self, svc_id): return self.deployments.get(svc_id, [])
    def list_env_vars(self, svc_id): return self.env_vars.get(svc_id, [])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_db_reconcile_lands_tables_columns_indexes_and_fks():
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")

    db_conn = DatabaseConnector(
        store=store, project_id=pid,
        db_artifact_key="db:supabase_main",
        client=FakeSupabase(),
    )
    summary = db_conn.reconcile()

    assert summary == {"tables": 2, "columns": 7, "indexes": 2}, \
        f"reconcile summary mismatch: {summary}"

    # Verify FK edge: boards.owner_id -> users.id
    edges = store.neighbors(
        pid, "column:db:supabase_main:boards.owner_id",
        direction="out", edge_kinds=[EdgeKind("fk_references")],
    )
    assert len(edges) == 1
    assert edges[0].to_artifact_key == "column:db:supabase_main:users.id"

    print("  ✓ db_reconcile_lands_tables_columns_indexes_and_fks")


def test_symbol_extraction_finds_function_to_column_reads_and_writes():
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")

    db_conn = DatabaseConnector(
        store, pid, "db:supabase_main", FakeSupabase(),
    )
    db_conn.reconcile()

    git_conn = GitConnector(
        store=store, project_id=pid,
        repo="hugh/myapp", branch="main",
        client=FakeGithub(), db_connector=db_conn,
    )
    result = git_conn.ingest_commit(FakeGithub().commits[0])

    assert result["files_changed"] == 2
    assert result["symbols"] > 0
    assert result["column_refs"] > 0, "should find column refs in the TS code"

    # The getUserProfile function reads users.email (among others).
    # Find the symbol artifact_key.
    user_symbols = [
        e for e in store.all_current(pid, ArtifactKind("function_symbol"))
        if "users.ts" in e.artifact_key
    ]
    sym_keys = {e.artifact_key for e in user_symbols}
    assert any("getUserProfile" in k for k in sym_keys), \
        f"getUserProfile symbol missing; got: {sym_keys}"

    # Find the outgoing reads_column edges from getUserProfile.
    profile_key = next(k for k in sym_keys if "getUserProfile" in k)
    reads = store.neighbors(
        pid, profile_key, direction="out",
        edge_kinds=[EdgeKind("reads_column")],
    )
    columns_read = {e.to_artifact_key for e in reads}
    assert "column:db:supabase_main:users.email" in columns_read, \
        f"expected users.email in reads, got: {columns_read}"
    assert "column:db:supabase_main:users.legacy_username" in columns_read, \
        f"expected users.legacy_username in reads, got: {columns_read}"

    # And updateUserEmail should have a WRITE edge to users.email.
    update_key = next(k for k in sym_keys if "updateUserEmail" in k)
    writes = store.neighbors(
        pid, update_key, direction="out",
        edge_kinds=[EdgeKind("writes_column")],
    )
    columns_written = {e.to_artifact_key for e in writes}
    assert "column:db:supabase_main:users.email" in columns_written, \
        f"expected users.email in writes, got: {columns_written}"

    print("  ✓ symbol_extraction_finds_function_to_column_reads_and_writes")


def test_proposed_column_drop_surfaces_every_breaking_code_site():
    """The headline scenario: 'if I drop users.legacy_username, what breaks?'"""
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")

    db_conn = DatabaseConnector(store, pid, "db:supabase_main", FakeSupabase())
    db_conn.reconcile()

    git_conn = GitConnector(
        store, pid, "hugh/myapp", "main", FakeGithub(), db_conn,
    )
    git_conn.ingest_commit(FakeGithub().commits[0])

    analyzer = ChangeImpactAnalyzer(store, pid)
    report = analyzer.analyze_proposed_db_change(
        db_artifact_key="db:supabase_main",
        change_type="drop_column",
        target_table="users",
        target_column="legacy_username",
    )

    # The symbols that read legacy_username (getUserProfile and findByLegacy)
    # should be surfaced.
    affected_symbol_names = " ".join(report.affected_symbols)
    assert "getUserProfile" in affected_symbol_names, \
        f"getUserProfile not flagged; affected: {report.affected_symbols}"
    assert "findByLegacy" in affected_symbol_names, \
        f"findByLegacy not flagged; affected: {report.affected_symbols}"

    # The file that contains those symbols should be flagged too.
    assert any("users.ts" in f for f in report.affected_files), \
        f"users.ts not flagged; affected: {report.affected_files}"

    # The boards.ts file does NOT touch legacy_username and must not be flagged.
    assert not any("boards.ts" in f for f in report.affected_files), \
        f"boards.ts wrongly flagged: {report.affected_files}"

    # We expect at least one note about the read sites.
    assert any("READ" in n for n in report.notes), \
        f"missing read-sites note: {report.notes}"

    # Migration advice should appear.
    assert any("migration" in s.lower() for s in report.suggested_db_changes), \
        f"missing migration advice: {report.suggested_db_changes}"

    print("  ✓ proposed_column_drop_surfaces_every_breaking_code_site")


def test_deploy_in_impact_escalates_severity_to_breaking():
    """Once a service is deployed against the affected code, severity escalates."""
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")

    db_conn = DatabaseConnector(store, pid, "db:supabase_main", FakeSupabase())
    db_conn.reconcile()

    git_conn = GitConnector(
        store, pid, "hugh/myapp", "main", FakeGithub(), db_conn,
    )
    git_conn.ingest_commit(FakeGithub().commits[0])

    rail = RailwayConnector(
        store=store, project_id=pid,
        client=FakeRailway(repo="hugh/myapp", commit_sha="abc123def456"),
    )
    rail.reconcile()

    # The deploy node should now exist and link back to the commit, which
    # links back to the file, which links back to the symbols that read
    # legacy_username. So changing legacy_username should ripple all the way
    # up to a deploy — making severity 'breaking'.
    analyzer = ChangeImpactAnalyzer(store, pid)
    report = analyzer.analyze_proposed_db_change(
        "db:supabase_main", "drop_column", "users", "legacy_username",
    )

    assert report.severity == "breaking", \
        f"expected severity 'breaking', got '{report.severity}'. " \
        f"affected_deploys={report.affected_deploys}"
    assert report.affected_deploys, \
        "expected at least one affected deploy"

    print("  ✓ deploy_in_impact_escalates_severity_to_breaking")


def test_additive_change_reports_low_severity_and_forward_compatible():
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")
    db_conn = DatabaseConnector(store, pid, "db:supabase_main", FakeSupabase())
    db_conn.reconcile()

    analyzer = ChangeImpactAnalyzer(store, pid)
    report = analyzer.analyze_proposed_db_change(
        "db:supabase_main", "add_column", "users", "avatar_url",
    )

    assert report.severity == "low"
    assert "Forward-compatible" in " ".join(report.notes)

    print("  ✓ additive_change_reports_low_severity_and_forward_compatible")


def test_report_serializes_cleanly_for_ui():
    """The frontend will consume report.to_dict(); make sure it's JSON-safe."""
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")
    db_conn = DatabaseConnector(store, pid, "db:supabase_main", FakeSupabase())
    db_conn.reconcile()
    git_conn = GitConnector(
        store, pid, "hugh/myapp", "main", FakeGithub(), db_conn,
    )
    git_conn.ingest_commit(FakeGithub().commits[0])

    analyzer = ChangeImpactAnalyzer(store, pid)
    report = analyzer.analyze_proposed_db_change(
        "db:supabase_main", "rename_column", "users", "legacy_username",
    )

    payload = report.to_dict()
    # Round-trip through JSON to catch any non-serializable fields.
    json.dumps(payload)

    assert "target" in payload
    assert "affected" in payload
    assert "severity" in payload
    # rename_column should add the expand/contract advice.
    assert any("expand/contract" in s for s in report.suggested_db_changes), \
        f"missing expand/contract advice: {report.suggested_db_changes}"

    print("  ✓ report_serializes_cleanly_for_ui")


def run_all():
    tests = [
        test_db_reconcile_lands_tables_columns_indexes_and_fks,
        test_symbol_extraction_finds_function_to_column_reads_and_writes,
        test_proposed_column_drop_surfaces_every_breaking_code_site,
        test_deploy_in_impact_escalates_severity_to_breaking,
        test_additive_change_reports_low_severity_and_forward_compatible,
        test_report_serializes_cleanly_for_ui,
    ]
    print(f"Running {len(tests)} runtime_sync tests...")
    for t in tests:
        t()
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    run_all()
