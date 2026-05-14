"""
codeflow.ledger_memory
======================

In-memory implementation of the same API as LedgerStore, for tests and for
local development without standing up Postgres. The schemas and method
signatures match ledger.py exactly. Use this in unit tests; use LedgerStore
in production.

We intentionally re-implement instead of inheriting: the SQL store relies on
Postgres-specific features (recursive CTEs, partial indexes, ON CONFLICT) and
trying to share code between the two would force ugly abstractions. The two
implementations are validated against the same behavioral tests.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

from ledger import (
    ArtifactKind, EdgeKind, GraphEdge, LedgerEntry, NODE_KINDS, Tier,
    _infer_node_kind,
)


@dataclass
class _Blob:
    sha256: str
    content_type: str
    body: bytes


@dataclass
class _Node:
    id: str
    project_id: str
    artifact_key: str
    current_entry_id: Optional[str]
    node_kind: ArtifactKind


@dataclass
class _Edge:
    id: str
    project_id: str
    from_key: str
    to_key: str
    edge_kind: EdgeKind
    declared_in: str


@dataclass
class _ManifestItem:
    project_id: str
    item_key: str
    description: str
    category: str
    locked_at: Optional[datetime] = None


@dataclass
class _Project:
    id: str
    slug: str
    prompt: str
    status: str = "planning"


class InMemoryLedgerStore:
    """API-compatible with LedgerStore for testing."""

    def __init__(self) -> None:
        self._projects: dict[str, _Project] = {}
        self._blobs: dict[str, _Blob] = {}
        self._entries: list[LedgerEntry] = []
        self._nodes: dict[tuple[str, str], _Node] = {}  # (project_id, artifact_key)
        self._edges: list[_Edge] = []
        self._manifest: dict[tuple[str, str], _ManifestItem] = {}
        self._seq = itertools.count(1)

    # -- projects ----------------------------------------------------------

    def create_project(self, slug: str, prompt: str) -> str:
        pid = str(uuid.uuid4())
        self._projects[pid] = _Project(id=pid, slug=slug, prompt=prompt)
        return pid

    def set_project_status(self, project_id: str, status: str) -> None:
        self._projects[project_id].status = status

    def delete_project(self, project_id: str) -> bool:
        """In-memory equivalent: drop the project and all of its
        ledger entries / nodes / edges / etc."""
        if project_id not in self._projects:
            return False
        del self._projects[project_id]
        # Drop everything keyed by this project_id. Sweep each registry;
        # registries that don't key by project_id (e.g. blobs) are
        # intentionally left alone, matching the SQL impl.
        self._entries = [
            e for e in self._entries if e.project_id != project_id
        ]
        self._nodes = {
            k: v for k, v in self._nodes.items()
            if k[0] != project_id
        }
        self._edges = [
            e for e in self._edges if e.project_id != project_id
        ]
        self._manifest = {
            k: v for k, v in self._manifest.items()
            if k[0] != project_id
        }
        return True

    # -- core write --------------------------------------------------------

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
        # Normalize body — same rules as the SQL store.
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

        sha = hashlib.sha256(payload).hexdigest()
        self._blobs.setdefault(sha, _Blob(sha, ct, payload))

        # Find prior current entry to supersede.
        prior = self._current_entry_for(project_id, artifact_key)
        supersedes_id = prior.id if prior else None

        entry = LedgerEntry(
            id=str(uuid.uuid4()),
            project_id=project_id,
            seq=next(self._seq),
            tier=tier,
            artifact_kind=artifact_kind,
            artifact_key=artifact_key,
            blob_sha256=sha,
            rationale=rationale,
            author=author,
            supersedes_id=supersedes_id,
            created_at=datetime.now(timezone.utc),
        )
        self._entries.append(entry)

        # Maintain the graph.
        if artifact_kind in NODE_KINDS:
            self._upsert_node(project_id, artifact_key, entry.id, artifact_kind)

        # Collect edges from the shortcuts and explicit list.
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

        for edge_kind, target_key in edges:
            self._upsert_edge(
                project_id, artifact_key, target_key, edge_kind, entry.id
            )

        return entry

    def _upsert_node(
        self, project_id: str, artifact_key: str,
        current_entry_id: Optional[str], node_kind: ArtifactKind,
    ) -> None:
        key = (project_id, artifact_key)
        if key in self._nodes:
            if current_entry_id:
                self._nodes[key].current_entry_id = current_entry_id
        else:
            self._nodes[key] = _Node(
                id=str(uuid.uuid4()),
                project_id=project_id,
                artifact_key=artifact_key,
                current_entry_id=current_entry_id,
                node_kind=node_kind,
            )

    def _ensure_placeholder(self, project_id: str, artifact_key: str) -> None:
        key = (project_id, artifact_key)
        if key in self._nodes:
            return
        self._nodes[key] = _Node(
            id=str(uuid.uuid4()),
            project_id=project_id,
            artifact_key=artifact_key,
            current_entry_id=None,
            node_kind=_infer_node_kind(artifact_key),
        )

    def _upsert_edge(
        self, project_id: str, from_key: str, to_key: str,
        edge_kind: EdgeKind, declared_in: str,
    ) -> None:
        if (project_id, from_key) not in self._nodes:
            raise RuntimeError(f"from-node missing: {from_key}")
        self._ensure_placeholder(project_id, to_key)
        # Dedupe.
        for e in self._edges:
            if (e.project_id == project_id and e.from_key == from_key
                    and e.to_key == to_key and e.edge_kind == edge_kind):
                return
        self._edges.append(_Edge(
            id=str(uuid.uuid4()),
            project_id=project_id,
            from_key=from_key, to_key=to_key,
            edge_kind=edge_kind, declared_in=declared_in,
        ))

    # -- blobs -------------------------------------------------------------

    def get_blob(self, sha256: str) -> tuple[bytes, str]:
        """API-compatible with LedgerStore.get_blob. Returns (body, content_type)."""
        blob = self._blobs.get(sha256)
        if not blob:
            raise KeyError(sha256)
        return blob.body, blob.content_type

    # -- reads -------------------------------------------------------------

    def _current_entry_for(
        self, project_id: str, artifact_key: str,
    ) -> Optional[LedgerEntry]:
        superseded_ids = {e.supersedes_id for e in self._entries if e.supersedes_id}
        matches = [
            e for e in self._entries
            if e.project_id == project_id
            and e.artifact_key == artifact_key
            and e.id not in superseded_ids
        ]
        if not matches:
            return None
        return max(matches, key=lambda e: e.seq)

    def current_entry(
        self, project_id: str, artifact_key: str,
    ) -> Optional[LedgerEntry]:
        return self._current_entry_for(project_id, artifact_key)

    def history(
        self, project_id: str, artifact_key: str,
    ) -> list[LedgerEntry]:
        return sorted(
            [e for e in self._entries
             if e.project_id == project_id and e.artifact_key == artifact_key],
            key=lambda e: e.seq,
        )

    def all_current(
        self, project_id: str, artifact_kind: Optional[ArtifactKind] = None,
    ) -> list[LedgerEntry]:
        superseded_ids = {e.supersedes_id for e in self._entries if e.supersedes_id}
        out = [
            e for e in self._entries
            if e.project_id == project_id and e.id not in superseded_ids
        ]
        if artifact_kind:
            out = [e for e in out if e.artifact_kind == artifact_kind]
        return sorted(out, key=lambda e: e.seq)

    # -- graph queries -----------------------------------------------------

    def neighbors(
        self, project_id: str, artifact_key: str, *,
        direction: str = "out",
        edge_kinds: Optional[Iterable[EdgeKind]] = None,
    ) -> list[GraphEdge]:
        kinds = set(edge_kinds) if edge_kinds else None
        out = []
        for e in self._edges:
            if e.project_id != project_id:
                continue
            if kinds and e.edge_kind not in kinds:
                continue
            if direction == "out" and e.from_key != artifact_key:
                continue
            if direction == "in" and e.to_key != artifact_key:
                continue
            if direction == "both" and artifact_key not in (e.from_key, e.to_key):
                continue
            out.append(GraphEdge(
                id=e.id,
                from_artifact_key=e.from_key,
                to_artifact_key=e.to_key,
                edge_kind=e.edge_kind,
                declared_in=e.declared_in,
            ))
        return out

    def impact_set(
        self, project_id: str, artifact_key: str, *, max_depth: int = 10,
    ) -> set[str]:
        """BFS walking incoming edges. Cycles are tolerated via the visited set."""
        visited = {artifact_key}
        frontier = {artifact_key}
        depth = 0
        while frontier and depth < max_depth:
            next_frontier: set[str] = set()
            for key in frontier:
                for e in self._edges:
                    if (e.project_id == project_id
                            and e.to_key == key
                            and e.from_key not in visited):
                        next_frontier.add(e.from_key)
                        visited.add(e.from_key)
            frontier = next_frontier
            depth += 1
        return visited - {artifact_key}

    def orphans(self, project_id: str) -> list[str]:
        out = []
        for (pid, key), n in self._nodes.items():
            if pid != project_id:
                continue
            if n.node_kind == ArtifactKind.SPEC_MANIFEST_ITEM:
                continue
            has_incoming = any(
                e.project_id == project_id and e.to_key == key
                for e in self._edges
            )
            if not has_incoming:
                out.append(key)
        return out

    def unsatisfied_manifest_items(self, project_id: str) -> list[str]:
        out = []
        for (pid, item_key), m in self._manifest.items():
            if pid != project_id:
                continue
            manifest_node_key = f"manifest:{item_key}"
            satisfied = any(
                e.project_id == project_id
                and e.to_key == manifest_node_key
                and e.edge_kind == EdgeKind.SATISFIES_MANIFEST_ITEM
                for e in self._edges
            )
            if not satisfied:
                out.append(item_key)
        return sorted(out)

    # -- manifest ----------------------------------------------------------

    def add_manifest_item(
        self, project_id: str, item_key: str,
        description: str, category: str,
    ) -> None:
        self._manifest[(project_id, item_key)] = _ManifestItem(
            project_id=project_id, item_key=item_key,
            description=description, category=category,
        )
        # Mirror as a graph node so satisfaction edges can target it.
        self._upsert_node(
            project_id, f"manifest:{item_key}", None,
            ArtifactKind.SPEC_MANIFEST_ITEM,
        )

    def lock_manifest(self, project_id: str) -> None:
        now = datetime.now(timezone.utc)
        for (pid, _), item in self._manifest.items():
            if pid == project_id and item.locked_at is None:
                item.locked_at = now


# ============================================================================
# Tests
# ============================================================================

def _build_kanban_scenario():
    """A representative scenario: spec planner writes a manifest and entities,
    generators write files implementing them, audits run."""
    store = InMemoryLedgerStore()
    pid = store.create_project("kanban", "Build me a kanban app")

    # Gate 1: spec planner builds the manifest.
    for key, desc, cat in [
        ("entity:User",            "Users authenticate with email",       "entity"),
        ("entity:Board",           "Top-level container of columns",      "entity"),
        ("entity:Card",            "Movable item within a column",        "entity"),
        ("route:POST /boards",     "Create a new board",                  "route"),
        ("route:POST /cards/move", "Move a card across columns",          "route"),
        ("edge_case:auth_expired", "Token refresh on 401",                "edge_case"),
    ]:
        store.add_manifest_item(pid, key, desc, cat)

    # Spec entities (writes to ledger + graph).
    store.write_entry(
        pid, Tier.SPEC, ArtifactKind.SPEC_ENTITY, "entity:User",
        body={"fields": {"id": "uuid", "email": "string"}},
        rationale="Required by auth and ownership of boards",
        author="spec_planner",
    )
    store.write_entry(
        pid, Tier.SPEC, ArtifactKind.SPEC_ENTITY, "entity:Board",
        body={"fields": {"id": "uuid", "owner_id": "uuid", "title": "string"}},
        rationale="Owned by User",
        author="spec_planner",
        depends_on=["entity:User"],
    )
    store.lock_manifest(pid)

    # Tier 2: a generator writes the user model file, which implements
    # entity:User and satisfies the User manifest item.
    store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE, "file:src/models/user.ts",
        body="export interface User { id: string; email: string }",
        rationale="Implements entity:User",
        author="claude",
        implements=["entity:User"],
        satisfies_manifest_item=["manifest:entity:User"],
    )

    # A board model file that imports the user model.
    store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE, "file:src/models/board.ts",
        body="import {User} from './user'; export interface Board { id: string; owner: User }",
        rationale="Implements entity:Board",
        author="gpt-4",
        implements=["entity:Board"],
        imports=["file:src/models/user.ts"],
        satisfies_manifest_item=["manifest:entity:Board"],
    )

    # A route file that imports the board model.
    store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE, "file:src/api/boards.ts",
        body="// POST /boards handler",
        rationale="Implements POST /boards",
        author="claude",
        imports=["file:src/models/board.ts"],
        satisfies_manifest_item=["manifest:route:POST /boards"],
    )

    return store, pid


def test_supersession_keeps_history_and_swaps_current():
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")

    store.write_entry(
        pid, Tier.SPEC, ArtifactKind.SPEC_ENTITY, "entity:User",
        body={"v": 1}, rationale="initial", author="spec_planner",
    )
    store.write_entry(
        pid, Tier.SPEC, ArtifactKind.SPEC_ENTITY, "entity:User",
        body={"v": 2}, rationale="added oauth fields", author="spec_planner",
    )

    history = store.history(pid, "entity:User")
    assert len(history) == 2, "history should preserve both revisions"
    assert history[0].supersedes_id is None
    assert history[1].supersedes_id == history[0].id

    current = store.current_entry(pid, "entity:User")
    assert current.id == history[1].id, "current = latest non-superseded"
    assert current.rationale == "added oauth fields"

    print("  ✓ supersession_keeps_history_and_swaps_current")


def test_impact_set_walks_transitive_dependents():
    store, pid = _build_kanban_scenario()

    # If we change the user model, what else is affected?
    # board.ts imports user.ts; boards.ts imports board.ts.
    # So changing user.ts should bubble up to both.
    impact = store.impact_set(pid, "file:src/models/user.ts")

    assert "file:src/models/board.ts" in impact, "direct importer should be in impact set"
    assert "file:src/api/boards.ts" in impact, "transitive importer should be in impact set"
    assert "file:src/models/user.ts" not in impact, "the changing node itself excluded"

    # entity:User is ALSO upstream because file:src/models/user.ts implements it.
    # Wait — implements goes file -> entity, so entity:User is downstream, not upstream.
    # The impact_set walks INCOMING edges to user.ts, meaning "who points at user.ts".
    # entity:User does not point at user.ts; user.ts points at entity:User via 'implements'.
    # So entity:User should NOT be in the impact set.
    assert "entity:User" not in impact, \
        "entity:User is implemented BY user.ts, not a dependent OF it"

    print("  ✓ impact_set_walks_transitive_dependents")


def test_unsatisfied_manifest_items_finds_gaps():
    store, pid = _build_kanban_scenario()

    # We satisfied: entity:User, entity:Board, route:POST /boards.
    # We did NOT satisfy: entity:Card, route:POST /cards/move, edge_case:auth_expired.
    unsatisfied = set(store.unsatisfied_manifest_items(pid))

    assert unsatisfied == {
        "entity:Card",
        "route:POST /cards/move",
        "edge_case:auth_expired",
    }, f"unexpected gaps: {unsatisfied}"

    print("  ✓ unsatisfied_manifest_items_finds_gaps")


def test_orphans_finds_dead_code():
    store, pid = _build_kanban_scenario()

    # No file points at boards.ts; it's a route, intended to be an entry point.
    # The orphan check is conservative: every non-manifest node with no
    # incoming edge is reported. In a real Gate 0, routes get a pass via
    # being declared as entry points; we just verify the raw detection here.
    orphans = set(store.orphans(pid))

    # boards.ts has no incoming edge — flagged as orphan (route entry; would
    # be whitelisted by Gate 0 in production)
    assert "file:src/api/boards.ts" in orphans, \
        "files with no incoming edges should surface as orphans"

    # user.ts has incoming edge from board.ts; not an orphan.
    assert "file:src/models/user.ts" not in orphans

    print("  ✓ orphans_finds_dead_code")


def test_content_addressable_dedup():
    """Two writes of identical content share blob storage."""
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")

    e1 = store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE, "file:a.ts",
        body="export const x = 1", rationale="r", author="claude",
    )
    e2 = store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE, "file:b.ts",
        body="export const x = 1", rationale="r", author="gpt-4",
    )

    assert e1.blob_sha256 == e2.blob_sha256, \
        "identical bodies should produce identical hashes"
    assert len(store._blobs) == 1, "blob storage should dedupe"

    print("  ✓ content_addressable_dedup")


def test_forward_reference_creates_placeholder():
    """A generator can declare a dependency before the target is written."""
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")

    # File A imports File B, but B doesn't exist yet.
    store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE, "file:a.ts",
        body="import {} from './b'", rationale="r", author="claude",
        imports=["file:b.ts"],
    )

    # Placeholder node for b.ts should exist.
    nb = store._nodes.get((pid, "file:b.ts"))
    assert nb is not None, "placeholder should be created"
    assert nb.current_entry_id is None, "placeholder has no current entry yet"
    assert nb.node_kind == ArtifactKind.FILE

    # Now b.ts lands — placeholder gets filled in.
    eb = store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE, "file:b.ts",
        body="export const y = 2", rationale="r", author="claude",
    )
    nb_after = store._nodes[(pid, "file:b.ts")]
    assert nb_after.current_entry_id == eb.id, \
        "placeholder should be updated with the real entry id"

    print("  ✓ forward_reference_creates_placeholder")


def test_cycle_does_not_explode_impact_set():
    """If A imports B and B imports A, impact_set must terminate."""
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")

    store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE, "file:a.ts",
        body="x", rationale="r", author="claude",
        imports=["file:b.ts"],
    )
    store.write_entry(
        pid, Tier.GENERATION, ArtifactKind.FILE, "file:b.ts",
        body="y", rationale="r", author="claude",
        imports=["file:a.ts"],
    )

    impact = store.impact_set(pid, "file:a.ts")
    assert "file:b.ts" in impact
    # Critically, no infinite loop — we got a finite answer.
    print("  ✓ cycle_does_not_explode_impact_set")


def test_revision_after_audit_failure_supersedes_correctly():
    """Simulate Gate 1 rejecting a spec; the revision should supersede the
    original, current_entry() should return the new one, and history shows
    both."""
    store = InMemoryLedgerStore()
    pid = store.create_project("p", "test")

    # Initial spec entity.
    e1 = store.write_entry(
        pid, Tier.SPEC, ArtifactKind.SPEC_ENTITY, "entity:User",
        body={"fields": {"id": "uuid"}},
        rationale="initial draft",
        author="spec_planner",
    )

    # Gate 1 audit verdict (ledger-only artifact, not a graph node).
    store.write_entry(
        pid, Tier.AUDIT, ArtifactKind.AUDIT_VERDICT,
        "audit:gate1:entity:User",
        body={"verdict": "fail", "missing": ["email", "created_at"]},
        rationale="found gaps via gate 1",
        author="audit_panel_claude_gpt",
    )

    # Patch: revise spec with the missing fields.
    e2 = store.write_entry(
        pid, Tier.PATCH, ArtifactKind.SPEC_ENTITY, "entity:User",
        body={"fields": {"id": "uuid", "email": "string", "created_at": "timestamp"}},
        rationale="added email and created_at per audit",
        author="spec_planner",
    )

    assert e2.supersedes_id == e1.id
    current = store.current_entry(pid, "entity:User")
    assert current.id == e2.id

    hist = store.history(pid, "entity:User")
    assert [e.tier for e in hist] == [Tier.SPEC, Tier.PATCH]

    print("  ✓ revision_after_audit_failure_supersedes_correctly")


def run_all():
    tests = [
        test_supersession_keeps_history_and_swaps_current,
        test_impact_set_walks_transitive_dependents,
        test_unsatisfied_manifest_items_finds_gaps,
        test_orphans_finds_dead_code,
        test_content_addressable_dedup,
        test_forward_reference_creates_placeholder,
        test_cycle_does_not_explode_impact_set,
        test_revision_after_audit_failure_supersedes_correctly,
    ]
    print(f"Running {len(tests)} ledger tests...")
    for t in tests:
        t()
    print(f"All {len(tests)} tests passed.")


if __name__ == "__main__":
    run_all()
