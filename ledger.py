"""
codeflow.ledger
===============

The institutional memory of a Code Flow build. Every tier (spec, generation,
merge, audit, patch) writes ledger entries; every audit and every downstream
tier reads them. Alongside the ledger we maintain a typed dependency graph
that is the authoritative answer to "what depends on what".

Why these primitives matter
---------------------------
Multi-AI pipelines rot when each AI sees only the artifact in front of it and
has to reconstruct intent from syntax. The ledger gives every AI the full
chain of prior decisions plus rationale, so a Gate 3 auditor can say
"this contradicts the Tier 1 decision to use Zustand, justified or accident?"
The graph lets us compute ripple effects: when anything changes, we know
exactly which other artifacts to re-audit, so a fix never silently breaks
something three files away.

Usage shape
-----------
    store = LedgerStore(database_url=...)
    project_id = store.create_project(slug="kanban-demo", prompt="Build me a kanban app")

    # Tier 1: spec planner writes spec entries
    store.write_entry(
        project_id=project_id,
        tier=Tier.SPEC,
        artifact_kind=ArtifactKind.SPEC_ENTITY,
        artifact_key="entity:User",
        body={"fields": {"id": "uuid", "email": "string"}},
        rationale="Required by auth flow and audit log",
        author="spec_planner",
        depends_on=["spec_entity:db_postgres", "spec_entity:auth_jwt"],
    )

    # Tier 2: generator reads the full ledger as context, writes its file
    store.write_entry(
        project_id=project_id,
        tier=Tier.GENERATION,
        artifact_kind=ArtifactKind.FILE,
        artifact_key="file:src/models/user.ts",
        body=file_text,
        rationale="Implements entity:User with email-uniqueness validation",
        author="claude",
        implements=["entity:User"],
        imports=["file:src/lib/db.ts"],
    )

    # Anywhere: ripple analysis before a planned change
    impacted = store.impact_set(project_id, "file:src/models/user.ts")
    # -> set of artifact_keys that transitively depend on this file
"""

from __future__ import annotations

import enum
import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

import psycopg
from psycopg.rows import dict_row


# ---------------------------------------------------------------------------
# Typed enums. Every AI in the pipeline must use these — that's why they're
# enforced at the SQL level too (see schema.sql CHECK constraints).
# Adding a new value requires a migration plus a registered handler, which is
# the friction we want — it prevents drift across a multi-AI system.
# ---------------------------------------------------------------------------

class Tier(str, enum.Enum):
    SPEC = "spec"
    GENERATION = "generation"
    MERGE = "merge"
    AUDIT = "audit"
    PATCH = "patch"


class ArtifactKind(str, enum.Enum):
    SPEC_ENTITY = "spec_entity"
    SPEC_ROUTE = "spec_route"
    SPEC_CONTRACT = "spec_contract"
    SPEC_MANIFEST_ITEM = "spec_manifest_item"
    FILE = "file"
    CONTRACT = "contract"
    TEST_FIXTURE = "test_fixture"
    ENV_VAR = "env_var"
    AUDIT_VERDICT = "audit_verdict"
    VOTE_RECORD = "vote_record"
    DECISION_RECORD = "decision_record"
    # --- runtime sync extensions (v2) ---
    FUNCTION_SYMBOL = "function_symbol"
    FEATURE = "feature"
    DB_SCHEMA = "db_schema"
    DB_TABLE = "db_table"
    DB_COLUMN = "db_column"
    DB_INDEX = "db_index"
    DB_MIGRATION = "db_migration"
    GIT_REPO = "git_repo"
    GIT_BRANCH = "git_branch"
    GIT_COMMIT = "git_commit"
    GIT_PR = "git_pr"
    DEPLOY = "deploy"
    SERVICE = "service"
    # --- guardian extensions (v3) ---
    # Semantic summaries are the guardian's understanding of a code
    # artifact: what it means, what it assumes, what its failure modes
    # are. File-level summaries use key shape `semantic:<project>:<path>`;
    # symbol-level use `semantic_symbol:<project>:<path>:<symbol_name>`.
    # Not graph nodes — they ATTACH to existing nodes (file artifacts)
    # rather than introducing their own.
    SEMANTIC_SUMMARY = "semantic_summary"


# Subset of ArtifactKinds that can be nodes in the dependency graph.
# Audit verdicts and vote records are ledger-only; they do not represent
# runtime artifacts and so they don't earn graph nodes.
NODE_KINDS = frozenset({
    ArtifactKind.SPEC_ENTITY,
    ArtifactKind.SPEC_ROUTE,
    ArtifactKind.SPEC_CONTRACT,
    ArtifactKind.SPEC_MANIFEST_ITEM,
    ArtifactKind.FILE,
    ArtifactKind.CONTRACT,
    ArtifactKind.TEST_FIXTURE,
    ArtifactKind.ENV_VAR,
    # runtime sync nodes
    ArtifactKind.FUNCTION_SYMBOL,
    ArtifactKind.FEATURE,
    ArtifactKind.DB_SCHEMA,
    ArtifactKind.DB_TABLE,
    ArtifactKind.DB_COLUMN,
    ArtifactKind.DB_INDEX,
    ArtifactKind.DB_MIGRATION,
    ArtifactKind.GIT_REPO,
    ArtifactKind.GIT_BRANCH,
    ArtifactKind.GIT_COMMIT,
    ArtifactKind.GIT_PR,
    ArtifactKind.DEPLOY,
    ArtifactKind.SERVICE,
})


class EdgeKind(str, enum.Enum):
    IMPLEMENTS = "implements"                # file implements spec_entity
    IMPORTS = "imports"                      # file imports file
    CALLS = "calls"                          # file calls function in file
    READS_SCHEMA = "reads_schema"            # file reads spec_entity shape
    WRITES_SCHEMA = "writes_schema"          # file mutates spec_entity shape
    HONORS_CONTRACT = "honors_contract"      # file conforms to contract
    TESTS = "tests"                          # test_fixture exercises file
    DEPENDS_ON = "depends_on"                # generic; prefer specific kinds
    SATISFIES_MANIFEST_ITEM = "satisfies_manifest_item"  # file delivers manifest item
    # --- runtime sync extensions (v2) ---
    CONTAINS_SYMBOL = "contains_symbol"
    INVOKES = "invokes"
    REALIZES_FEATURE = "realizes_feature"
    HAS_TABLE = "has_table"
    HAS_COLUMN = "has_column"
    HAS_INDEX = "has_index"
    READS_COLUMN = "reads_column"
    WRITES_COLUMN = "writes_column"
    FK_REFERENCES = "fk_references"
    DEFINED_IN_COMMIT = "defined_in_commit"
    DEPLOYED_IN = "deployed_in"
    RUNS_SERVICE = "runs_service"
    BINDS_ENV_VAR = "binds_env_var"
    APPLIED_MIGRATION = "applied_migration"


# ---------------------------------------------------------------------------
# Dataclasses returned to callers. These are deliberately simple — they mirror
# the SQL rows. We don't use Pydantic here to keep this layer dependency-light;
# callers can wrap with Pydantic at the API boundary if they want validation.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LedgerEntry:
    id: str
    project_id: str
    seq: int
    tier: Tier
    artifact_kind: ArtifactKind
    artifact_key: str
    blob_sha256: str
    rationale: str
    author: str
    supersedes_id: Optional[str]
    created_at: Any           # datetime


@dataclass(frozen=True)
class GraphNode:
    id: str
    project_id: str
    artifact_key: str
    current_entry_id: str
    node_kind: ArtifactKind


@dataclass(frozen=True)
class GraphEdge:
    id: str
    from_artifact_key: str
    to_artifact_key: str
    edge_kind: EdgeKind
    declared_in: str          # ledger_entries.id


@dataclass
class EntryDraft:
    """
    What a caller passes to write_entry(). The store handles hashing the body,
    upserting the blob, writing the ledger row, and applying graph mutations
    atomically.

    Edge declarations are kept as a flat list so they map cleanly to SQL rows.
    For convenience, write_entry() also accepts shortcut kwargs like
    implements=[...] and imports=[...] which expand to (kind, target) tuples.
    """
    tier: Tier
    artifact_kind: ArtifactKind
    artifact_key: str
    body: bytes
    content_type: str
    rationale: str
    author: str
    edges: list[tuple[EdgeKind, str]] = field(default_factory=list)
    supersedes_id: Optional[str] = None


# ---------------------------------------------------------------------------
# The store. One instance per process is fine; psycopg's connection is opened
# per call and closed via context manager so we never hold a connection across
# AI calls (which can be slow).
# ---------------------------------------------------------------------------

class LedgerStore:
    """
    All ledger and graph operations. Every public method is a single
    transaction unless documented otherwise.
    """

    def __init__(self, database_url: str):
        self._dsn = database_url

    # -- connections -------------------------------------------------------

    def _connect(self) -> psycopg.Connection:
        # row_factory=dict_row lets us return rows as dicts which we then
        # construct into dataclasses. autocommit=False because every public
        # method is a transaction.
        return psycopg.connect(self._dsn, row_factory=dict_row, autocommit=False)

    # -- projects ----------------------------------------------------------

    def create_project(self, slug: str, prompt: str) -> str:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO projects (slug, prompt) VALUES (%s, %s) RETURNING id",
                (slug, prompt),
            )
            project_id = cur.fetchone()["id"]
            conn.commit()
            return str(project_id)

    def set_project_status(self, project_id: str, status: str) -> None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "UPDATE projects SET status = %s WHERE id = %s",
                (status, project_id),
            )
            conn.commit()

    # -- artifact blobs ----------------------------------------------------

    @staticmethod
    def _sha256(body: bytes) -> str:
        return hashlib.sha256(body).hexdigest()

    def _upsert_blob(
        self, cur: psycopg.Cursor, body: bytes, content_type: str
    ) -> str:
        """Returns the SHA-256 of the body. Idempotent — duplicate bodies
        share storage."""
        sha = self._sha256(body)
        cur.execute(
            """
            INSERT INTO artifact_blobs (sha256, content_type, body, byte_size)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (sha256) DO NOTHING
            """,
            (sha, content_type, body, len(body)),
        )
        return sha

    # -- the central write operation ---------------------------------------

    def write_entry(
        self,
        project_id: str,
        tier: Tier,
        artifact_kind: ArtifactKind,
        artifact_key: str,
        body: Any,
        rationale: str,
        author: str,
        *,
        content_type: Optional[str] = None,
        implements: Optional[Iterable[str]] = None,
        imports: Optional[Iterable[str]] = None,
        calls: Optional[Iterable[str]] = None,
        honors_contract: Optional[Iterable[str]] = None,
        tests: Optional[Iterable[str]] = None,
        depends_on: Optional[Iterable[str]] = None,
        satisfies_manifest_item: Optional[Iterable[str]] = None,
        explicit_edges: Optional[list[tuple[EdgeKind, str]]] = None,
    ) -> LedgerEntry:
        """
        Write a ledger entry and apply all graph mutations in one transaction.

        Body can be bytes, str, or a JSON-serializable object. Content type is
        inferred when not given: dict/list -> application/json,
        str -> text/plain, bytes -> application/octet-stream.

        Edge shortcuts (implements=, imports=, ...) are conveniences that
        expand to typed edges. For unusual edge kinds, pass explicit_edges.
        """
        # ---- normalize the body to bytes + content_type ----
        if isinstance(body, (dict, list)):
            payload = json.dumps(body, sort_keys=True).encode("utf-8")
            ct = content_type or "application/json"
        elif isinstance(body, str):
            payload = body.encode("utf-8")
            ct = content_type or "text/plain"
        elif isinstance(body, bytes):
            payload = body
            ct = content_type or "application/octet-stream"
        else:
            raise TypeError(f"unsupported body type: {type(body).__name__}")

        # ---- collect declared edges ----
        edges: list[tuple[EdgeKind, str]] = list(explicit_edges or [])
        for kind, targets in [
            (EdgeKind.IMPLEMENTS, implements),
            (EdgeKind.IMPORTS, imports),
            (EdgeKind.CALLS, calls),
            (EdgeKind.HONORS_CONTRACT, honors_contract),
            (EdgeKind.TESTS, tests),
            (EdgeKind.DEPENDS_ON, depends_on),
            (EdgeKind.SATISFIES_MANIFEST_ITEM, satisfies_manifest_item),
        ]:
            if targets:
                edges.extend((kind, t) for t in targets)

        with self._connect() as conn:
            with conn.cursor() as cur:
                # 1. Upsert the blob.
                sha = self._upsert_blob(cur, payload, ct)

                # 2. Find the prior current entry for this artifact_key
                #    (if any). The new row will supersede it.
                cur.execute(
                    """
                    SELECT id FROM ledger_entries
                    WHERE project_id = %s AND artifact_key = %s
                      AND id NOT IN (
                          SELECT supersedes_id FROM ledger_entries
                          WHERE supersedes_id IS NOT NULL
                            AND project_id = %s
                      )
                    ORDER BY seq DESC LIMIT 1
                    """,
                    (project_id, artifact_key, project_id),
                )
                prior = cur.fetchone()
                supersedes_id = prior["id"] if prior else None

                # 3. Insert the ledger entry.
                cur.execute(
                    """
                    INSERT INTO ledger_entries
                        (project_id, tier, artifact_kind, artifact_key,
                         blob_sha256, rationale, author, supersedes_id)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, project_id, seq, tier, artifact_kind,
                              artifact_key, blob_sha256, rationale, author,
                              supersedes_id, created_at
                    """,
                    (
                        project_id, tier.value, artifact_kind.value,
                        artifact_key, sha, rationale, author, supersedes_id,
                    ),
                )
                row = cur.fetchone()
                entry_id = row["id"]

                # 4. Update the graph if this artifact kind is a node kind.
                if artifact_kind in NODE_KINDS:
                    self._upsert_node(
                        cur, project_id, artifact_key, entry_id, artifact_kind
                    )

                # 5. Apply edges. Edges target nodes by artifact_key; if a
                #    target node doesn't exist yet, we create a placeholder
                #    that will be filled in when the target is written.
                #    This lets generators declare dependencies before all
                #    files exist (forward references).
                for edge_kind, target_key in edges:
                    self._upsert_edge(
                        cur, project_id, artifact_key, target_key,
                        edge_kind, entry_id,
                    )

                conn.commit()

                return LedgerEntry(
                    id=str(row["id"]),
                    project_id=str(row["project_id"]),
                    seq=row["seq"],
                    tier=Tier(row["tier"]),
                    artifact_kind=ArtifactKind(row["artifact_kind"]),
                    artifact_key=row["artifact_key"],
                    blob_sha256=row["blob_sha256"],
                    rationale=row["rationale"],
                    author=row["author"],
                    supersedes_id=str(row["supersedes_id"]) if row["supersedes_id"] else None,
                    created_at=row["created_at"],
                )

    # -- graph mutations (internal) ----------------------------------------

    def _upsert_node(
        self, cur: psycopg.Cursor, project_id: str, artifact_key: str,
        current_entry_id: str, node_kind: ArtifactKind,
    ) -> str:
        """Insert the node if missing, or update its current_entry_id."""
        cur.execute(
            """
            INSERT INTO graph_nodes
                (project_id, artifact_key, current_entry_id, node_kind)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (project_id, artifact_key)
            DO UPDATE SET current_entry_id = EXCLUDED.current_entry_id
            RETURNING id
            """,
            (project_id, artifact_key, current_entry_id, node_kind.value),
        )
        return cur.fetchone()["id"]

    def _ensure_placeholder_node(
        self, cur: psycopg.Cursor, project_id: str, artifact_key: str,
    ) -> str:
        """
        Edges may declare a target that hasn't been written yet. We don't
        know its node_kind, so we infer it from the artifact_key prefix:
        'file:...' -> file, 'entity:...' -> spec_entity, etc.
        If a real write_entry() lands later, _upsert_node() replaces the
        placeholder's current_entry_id with the real one.
        """
        node_kind = _infer_node_kind(artifact_key)
        cur.execute(
            """
            INSERT INTO graph_nodes
                (project_id, artifact_key, current_entry_id, node_kind)
            VALUES (%s, %s, NULL, %s)
            ON CONFLICT (project_id, artifact_key) DO NOTHING
            RETURNING id
            """,
            (project_id, artifact_key, node_kind.value),
        )
        row = cur.fetchone()
        if row:
            return row["id"]
        cur.execute(
            "SELECT id FROM graph_nodes WHERE project_id = %s AND artifact_key = %s",
            (project_id, artifact_key),
        )
        return cur.fetchone()["id"]

    def _upsert_edge(
        self, cur: psycopg.Cursor, project_id: str,
        from_key: str, to_key: str, edge_kind: EdgeKind,
        declared_in: str,
    ) -> None:
        # Resolve both endpoints — creating a placeholder for the target if
        # it hasn't been written yet (forward references are allowed).
        cur.execute(
            "SELECT id FROM graph_nodes WHERE project_id = %s AND artifact_key = %s",
            (project_id, from_key),
        )
        from_row = cur.fetchone()
        if not from_row:
            # The from-node must exist by now; the caller is mid-write_entry()
            # so we shouldn't see this. Defensive raise.
            raise RuntimeError(
                f"from-node missing for edge: {from_key} -> {to_key}"
            )
        from_id = from_row["id"]
        to_id = self._ensure_placeholder_node(cur, project_id, to_key)

        cur.execute(
            """
            INSERT INTO graph_edges
                (project_id, from_node_id, to_node_id, edge_kind, declared_in)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (project_id, from_node_id, to_node_id, edge_kind)
            DO NOTHING
            """,
            (project_id, from_id, to_id, edge_kind.value, declared_in),
        )

    # -- reads -------------------------------------------------------------

    def get_blob(self, sha256: str) -> tuple[bytes, str]:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT body, content_type FROM artifact_blobs WHERE sha256 = %s",
                (sha256,),
            )
            row = cur.fetchone()
            if not row:
                raise KeyError(sha256)
            return bytes(row["body"]), row["content_type"]

    def current_entry(
        self, project_id: str, artifact_key: str
    ) -> Optional[LedgerEntry]:
        """Return the most recent non-superseded entry for an artifact, or None."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM current_artifacts
                WHERE project_id = %s AND artifact_key = %s
                """,
                (project_id, artifact_key),
            )
            row = cur.fetchone()
            if not row:
                return None
            return _row_to_entry(row)

    def history(
        self, project_id: str, artifact_key: str
    ) -> list[LedgerEntry]:
        """Full revision chain for an artifact, oldest first."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT * FROM ledger_entries
                WHERE project_id = %s AND artifact_key = %s
                ORDER BY seq ASC
                """,
                (project_id, artifact_key),
            )
            return [_row_to_entry(r) for r in cur.fetchall()]

    def all_current(
        self, project_id: str, artifact_kind: Optional[ArtifactKind] = None,
    ) -> list[LedgerEntry]:
        """All current artifacts for a project, optionally filtered by kind.
        This is the canonical 'give me the full current state of the build'
        query, used by audits to ground their context."""
        sql = "SELECT * FROM current_artifacts WHERE project_id = %s"
        params: list[Any] = [project_id]
        if artifact_kind:
            sql += " AND artifact_kind = %s"
            params.append(artifact_kind.value)
        sql += " ORDER BY seq ASC"
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(sql, params)
            return [_row_to_entry(r) for r in cur.fetchall()]

    # -- graph queries -----------------------------------------------------

    def neighbors(
        self,
        project_id: str,
        artifact_key: str,
        *,
        direction: str = "out",          # 'out' | 'in' | 'both'
        edge_kinds: Optional[Iterable[EdgeKind]] = None,
    ) -> list[GraphEdge]:
        """Direct neighbors of a node. The bread-and-butter graph query."""
        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be out|in|both")
        with self._connect() as conn, conn.cursor() as cur:
            edge_filter = ""
            params: list[Any] = [project_id, artifact_key]
            if edge_kinds:
                edge_filter = " AND e.edge_kind = ANY(%s)"
                params.append([k.value for k in edge_kinds])

            base = """
                SELECT e.id, e.edge_kind, e.declared_in,
                       nfrom.artifact_key AS from_key,
                       nto.artifact_key   AS to_key
                FROM graph_edges e
                JOIN graph_nodes nfrom ON nfrom.id = e.from_node_id
                JOIN graph_nodes nto   ON nto.id   = e.to_node_id
                WHERE e.project_id = %s
            """
            if direction == "out":
                sql = base + " AND nfrom.artifact_key = %s" + edge_filter
            elif direction == "in":
                sql = base + " AND nto.artifact_key = %s" + edge_filter
            else:
                sql = (
                    base + " AND (nfrom.artifact_key = %s OR nto.artifact_key = %s)"
                    + edge_filter
                )
                params = [project_id, artifact_key, artifact_key]
                if edge_kinds:
                    params.append([k.value for k in edge_kinds])

            cur.execute(sql, params)
            return [
                GraphEdge(
                    id=str(r["id"]),
                    from_artifact_key=r["from_key"],
                    to_artifact_key=r["to_key"],
                    edge_kind=EdgeKind(r["edge_kind"]),
                    declared_in=str(r["declared_in"]),
                )
                for r in cur.fetchall()
            ]

    def impact_set(
        self, project_id: str, artifact_key: str, *, max_depth: int = 10,
    ) -> set[str]:
        """
        Every artifact_key that transitively depends on the given key.
        Used before any planned change to a node: re-audit everything in
        the impact set, not just the changed node.

        Walks INCOMING edges — "what points at me?" — because if I change,
        the things that depend on me are what break.

        Uses a recursive CTE with a depth limit to avoid runaway traversals
        in pathological graphs (cycles are tolerated; depth caps them).
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                WITH RECURSIVE upstream AS (
                    SELECT n.id, n.artifact_key, 0 AS depth
                    FROM graph_nodes n
                    WHERE n.project_id = %s AND n.artifact_key = %s

                    UNION

                    SELECT n.id, n.artifact_key, u.depth + 1
                    FROM upstream u
                    JOIN graph_edges e   ON e.to_node_id = u.id
                                        AND e.project_id = %s
                    JOIN graph_nodes n   ON n.id = e.from_node_id
                    WHERE u.depth < %s
                )
                SELECT DISTINCT artifact_key FROM upstream
                WHERE artifact_key <> %s
                """,
                (project_id, artifact_key, project_id, max_depth, artifact_key),
            )
            return {r["artifact_key"] for r in cur.fetchall()}

    def orphans(self, project_id: str) -> list[str]:
        """
        Nodes that no edge points at AND that are not manifest items.
        These are the "defined but never used" findings Gate 0 surfaces.
        Manifest items are intentionally orphan-from-incoming because they
        are the start of every coverage chain.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT n.artifact_key
                FROM graph_nodes n
                WHERE n.project_id = %s
                  AND n.node_kind <> 'spec_manifest_item'
                  AND NOT EXISTS (
                      SELECT 1 FROM graph_edges e
                      WHERE e.to_node_id = n.id AND e.project_id = %s
                  )
                """,
                (project_id, project_id),
            )
            return [r["artifact_key"] for r in cur.fetchall()]

    def unsatisfied_manifest_items(self, project_id: str) -> list[str]:
        """
        Manifest items that have NO incoming 'satisfies_manifest_item' edge.
        These are the "feature is missing from the running app" findings
        Gate 0 surfaces. This is the concrete measurement behind 'nothing
        is missing'.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT m.item_key
                FROM manifest_items m
                WHERE m.project_id = %s
                  AND NOT EXISTS (
                      SELECT 1
                      FROM graph_nodes n
                      JOIN graph_edges e ON e.to_node_id = n.id
                      WHERE n.project_id = m.project_id
                        AND n.artifact_key = 'manifest:' || m.item_key
                        AND e.edge_kind = 'satisfies_manifest_item'
                  )
                ORDER BY m.item_key
                """,
                (project_id,),
            )
            return [r["item_key"] for r in cur.fetchall()]

    # -- manifest API ------------------------------------------------------

    def add_manifest_item(
        self,
        project_id: str,
        item_key: str,
        description: str,
        category: str,
    ) -> None:
        """Insert one manifest entry. Gate 1 calls this repeatedly while
        building the locked checklist, then calls lock_manifest()."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO manifest_items
                    (project_id, item_key, description, category)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (project_id, item_key) DO NOTHING
                """,
                (project_id, item_key, description, category),
            )
            # Mirror the manifest item into the graph so satisfaction edges
            # can target it. We do this here, not inside write_entry, because
            # manifest items don't have rich bodies — only the row itself.
            cur.execute(
                """
                INSERT INTO graph_nodes
                    (project_id, artifact_key, current_entry_id, node_kind)
                VALUES (%s, %s, NULL, 'spec_manifest_item')
                ON CONFLICT (project_id, artifact_key) DO NOTHING
                """,
                (project_id, f"manifest:{item_key}"),
            )
            conn.commit()

    def lock_manifest(self, project_id: str) -> None:
        """Mark every manifest item as locked. Gate 1 calls this once the
        spec audit verdict is 'pass'."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                UPDATE manifest_items SET locked_at = now()
                WHERE project_id = %s AND locked_at IS NULL
                """,
                (project_id,),
            )
            conn.commit()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_KEY_PREFIX_TO_NODE_KIND = {
    "entity":   ArtifactKind.SPEC_ENTITY,
    "route":    ArtifactKind.SPEC_ROUTE,
    "contract": ArtifactKind.SPEC_CONTRACT,
    "manifest": ArtifactKind.SPEC_MANIFEST_ITEM,
    "file":     ArtifactKind.FILE,
    "test":     ArtifactKind.TEST_FIXTURE,
    "env":      ArtifactKind.ENV_VAR,
    # runtime sync (v2)
    "func":     ArtifactKind.FUNCTION_SYMBOL,
    "feature":  ArtifactKind.FEATURE,
    "db":       ArtifactKind.DB_SCHEMA,
    "table":    ArtifactKind.DB_TABLE,
    "column":   ArtifactKind.DB_COLUMN,
    "index":    ArtifactKind.DB_INDEX,
    "migration": ArtifactKind.DB_MIGRATION,
    "repo":     ArtifactKind.GIT_REPO,
    "branch":   ArtifactKind.GIT_BRANCH,
    "commit":   ArtifactKind.GIT_COMMIT,
    "pr":       ArtifactKind.GIT_PR,
    "deploy":   ArtifactKind.DEPLOY,
    "service":  ArtifactKind.SERVICE,
    "ref":      ArtifactKind.DECISION_RECORD,
}


def _infer_node_kind(artifact_key: str) -> ArtifactKind:
    """Pick a node_kind from the artifact_key prefix. Convention beats
    configuration here — every layer must use 'file:...', 'entity:...', etc."""
    prefix = artifact_key.split(":", 1)[0]
    if prefix not in _KEY_PREFIX_TO_NODE_KIND:
        raise ValueError(
            f"unknown artifact_key prefix '{prefix}'. "
            f"Expected one of: {sorted(_KEY_PREFIX_TO_NODE_KIND)}"
        )
    return _KEY_PREFIX_TO_NODE_KIND[prefix]


def _row_to_entry(row: dict) -> LedgerEntry:
    return LedgerEntry(
        id=str(row["id"]),
        project_id=str(row["project_id"]),
        seq=row["seq"],
        tier=Tier(row["tier"]),
        artifact_kind=ArtifactKind(row["artifact_kind"]),
        artifact_key=row["artifact_key"],
        blob_sha256=row["blob_sha256"],
        rationale=row["rationale"],
        author=row["author"],
        supersedes_id=str(row["supersedes_id"]) if row["supersedes_id"] else None,
        created_at=row["created_at"],
    )
