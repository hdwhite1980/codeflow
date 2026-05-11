"""
codeflow.kinds_v2
=================

Extensions to the artifact_kind and edge_kind vocabulary for runtime sync.
These extend (not replace) the enums in ledger.py. In production, you'd merge
these into the same Enum class plus a Postgres migration that widens the
CHECK constraints. We keep them separate here so the diff is readable.

Why a "kinds" module exists at all
----------------------------------
A multi-AI pipeline that talks to GitHub, a serverless DB, and a host
platform will rot fast if every integration invents its own vocabulary. The
ledger's CHECK constraints are the canary — if a connector wants to write
something the enum doesn't cover, that's a deliberate review moment, not a
shrug. The enum is the contract between the AI layer and the infrastructure.
"""

from __future__ import annotations

import enum


class ArtifactKindV2(str, enum.Enum):
    # --- inherited from v1 (re-declared for completeness) ---
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

    # --- code structure (line-level via symbols) ---
    # A function, method, class, or route handler. The unit of "what does
    # this line do" — every line in a file belongs to exactly one symbol
    # (or to the file's module scope, which we model as a synthetic symbol).
    FUNCTION_SYMBOL = "function_symbol"
    # A logical feature surface: "user can move a card", "admin can export
    # report". Bridges manifest items (intent) to function symbols (mechanism).
    FEATURE = "feature"

    # --- database state ---
    DB_SCHEMA = "db_schema"          # a database (e.g. supabase project)
    DB_TABLE = "db_table"            # a table within a schema
    DB_COLUMN = "db_column"          # a column within a table
    DB_INDEX = "db_index"            # an index
    DB_MIGRATION = "db_migration"    # a migration plan or applied migration

    # --- version control ---
    GIT_REPO = "git_repo"
    GIT_BRANCH = "git_branch"
    GIT_COMMIT = "git_commit"        # an individual commit; immutable by nature
    GIT_PR = "git_pr"

    # --- deploy / hosting ---
    DEPLOY = "deploy"                # a single deployment of a commit
    SERVICE = "service"              # a long-running service (a railway service)


class EdgeKindV2(str, enum.Enum):
    # --- inherited ---
    IMPLEMENTS = "implements"
    IMPORTS = "imports"
    CALLS = "calls"
    READS_SCHEMA = "reads_schema"
    WRITES_SCHEMA = "writes_schema"
    HONORS_CONTRACT = "honors_contract"
    TESTS = "tests"
    DEPENDS_ON = "depends_on"
    SATISFIES_MANIFEST_ITEM = "satisfies_manifest_item"

    # --- code structure ---
    CONTAINS_SYMBOL = "contains_symbol"   # file -> function_symbol
    INVOKES = "invokes"                   # function_symbol -> function_symbol
    REALIZES_FEATURE = "realizes_feature" # function_symbol -> feature

    # --- database edges ---
    HAS_TABLE = "has_table"            # db_schema -> db_table
    HAS_COLUMN = "has_column"          # db_table -> db_column
    HAS_INDEX = "has_index"            # db_table -> db_index
    READS_COLUMN = "reads_column"      # function_symbol -> db_column
    WRITES_COLUMN = "writes_column"    # function_symbol -> db_column
    FK_REFERENCES = "fk_references"    # db_column -> db_column

    # --- git / deploy ---
    DEFINED_IN_COMMIT = "defined_in_commit"  # file/symbol -> git_commit
    DEPLOYED_IN = "deployed_in"              # commit -> deploy
    RUNS_SERVICE = "runs_service"            # deploy -> service
    BINDS_ENV_VAR = "binds_env_var"          # service -> env_var
    APPLIED_MIGRATION = "applied_migration"  # deploy -> db_migration


# Key prefix convention for the new kinds. The existing convention in
# ledger.py uses prefixes like "file:", "entity:", "manifest:". We extend it.
KEY_PREFIXES_V2 = {
    "func":       ArtifactKindV2.FUNCTION_SYMBOL,
    "feature":    ArtifactKindV2.FEATURE,
    "db":         ArtifactKindV2.DB_SCHEMA,
    "table":      ArtifactKindV2.DB_TABLE,
    "column":     ArtifactKindV2.DB_COLUMN,
    "index":      ArtifactKindV2.DB_INDEX,
    "migration":  ArtifactKindV2.DB_MIGRATION,
    "repo":       ArtifactKindV2.GIT_REPO,
    "branch":     ArtifactKindV2.GIT_BRANCH,
    "commit":     ArtifactKindV2.GIT_COMMIT,
    "pr":         ArtifactKindV2.GIT_PR,
    "deploy":     ArtifactKindV2.DEPLOY,
    "service":    ArtifactKindV2.SERVICE,
}
