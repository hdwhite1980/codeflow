/**
 * Shared TypeScript types matching the FastAPI response shapes.
 *
 * Keep these aligned with the corresponding backend endpoints in app.py.
 * If the backend changes shape, this file is the place to update —
 * everything else in the frontend reads through these types.
 */

// -- /api/projects --------------------------------------------------

export type ProjectStatus =
  | "planning"
  | "generating"
  | "merging"
  | "building"
  | "auditing"
  | "shipped"
  | "failed"
  | "requires_review";

export interface ProjectSummary {
  id: string;
  slug: string;
  prompt: string;
  status: ProjectStatus;
  created_at: string; // ISO 8601
}

export interface ProjectListResponse {
  projects: ProjectSummary[];
  count: number;
}

// -- POST /api/projects ---------------------------------------------

/**
 * One external service the user has wired their app to. These appear
 * as nodes in the graph view (Postgres, Railway, Vercel, GitHub etc.)
 * and let the frontend visualize the deploy/runtime topology.
 *
 * `kind` must be one of the values in ALLOWED_SERVICE_KINDS (backend
 * rejects unknown kinds with a 422). `label` is the display name in
 * the graph node. `config` is opaque metadata — connection string,
 * service URL, repo URL — that the frontend shows in the side panel
 * but doesn't otherwise interpret.
 */
export type ServiceKind =
  | "postgres"
  | "mysql"
  | "redis"
  | "railway"
  | "vercel"
  | "github"
  | "stripe"
  | "anthropic"
  | "openai"
  | "other";

export const ALLOWED_SERVICE_KINDS: ServiceKind[] = [
  "postgres",
  "mysql",
  "redis",
  "railway",
  "vercel",
  "github",
  "stripe",
  "anthropic",
  "openai",
  "other",
];

export interface ServiceConfig {
  kind: ServiceKind;
  label: string;
  config?: string | null;
}

export interface CreateProjectRequest {
  slug: string;
  prompt: string;
  /**
   * Mandatory: backend requires at least one service. The graph view
   * uses these as anchor nodes for the visualization.
   */
  services: ServiceConfig[];
}

export interface CreateProjectResponse {
  project_id: string;
  status?: string;
  slug?: string;
}

// -- /api/projects/<id>/artifacts -----------------------------------

export type ArtifactKind =
  | "spec_manifest_item"
  | "spec_entity"
  | "manifest"
  | "file"
  | "audit_verdict"
  | "decision_record";

export type ArtifactTier =
  | "spec"
  | "generation"
  | "merge"
  | "build"
  | "audit"
  | "runtime";

export interface ArtifactSummary {
  artifact_key: string;
  kind: ArtifactKind;
  tier: ArtifactTier;
  rationale: string;
  author: string;
  seq: number;
  created_at?: string;
}

export interface ArtifactListResponse {
  artifacts: ArtifactSummary[];
  count: number;
}

// -- /api/projects/<id>/usage ---------------------------------------

export interface UsageRow {
  id?: string;
  created_at: string;
  provider: string; // "anthropic" | "openai" | "google"
  model: string;
  stage: string; // "spec" | "file" | "audit"
  subject: string | null;
  input_tokens: number;
  output_tokens: number;
  input_cost_usd: number;
  output_cost_usd: number;
  total_tokens?: number;
  total_cost_usd: number;
}

export interface StageRollup {
  stage: string;
  call_count: number;
  input_tokens: number;
  output_tokens: number;
  input_cost_usd: number;
  output_cost_usd: number;
  total_tokens: number;
  total_cost_usd: number;
}

export interface UsageSummary {
  project_id: string;
  call_count: number;
  total_tokens: number;
  total_cost_usd: number;
  by_stage: StageRollup[];
  by_provider: StageRollup[]; // same shape; `stage` field holds provider name
  rows: UsageRow[];
}

// -- /api/projects/<id>/audits --------------------------------------

export type Severity = "critical" | "warning" | "nit";
export type Category =
  | "security"
  | "correctness"
  | "style"
  | "performance"
  | "other";

export interface Finding {
  severity: Severity;
  category: Category;
  line: number | null;
  issue: string;
  suggested_fix: string | null;
}

export interface AuditVerdict {
  artifact_key: string;
  auditor: string; // "openai" | "gemini"
  file_path: string;
  findings: Finding[];
  finding_count: number;
  parse_error: boolean;
  rationale: string;
  seq: number;
}

export interface AuditAuditorRollup {
  auditor: string;
  critical: number;
  warning: number;
  nit: number;
  files_audited: number;
}

export interface AuditResponse {
  project_id: string;
  verdict_count: number;
  total_findings: number;
  by_severity: { critical: number; warning: number; nit: number };
  by_auditor: AuditAuditorRollup[];
  parse_errors: number;
  verdicts: AuditVerdict[];
}

// -- WebSocket events -----------------------------------------------
//
// The /ws/projects/<id> endpoint streams JSON frames. Each frame has
// a `kind` discriminator. Frontend dispatches on it.

export interface HelloEvent {
  kind: "hello";
  project_id: string;
  message: string;
}

export interface LedgerEntryEvent {
  kind: "ledger_entry";
  project_id: string;
  data: ArtifactSummary;
}

export interface UsageRowEvent {
  kind: "usage_row";
  project_id: string;
  data: UsageRow;
}

export type WSEvent = HelloEvent | LedgerEntryEvent | UsageRowEvent;

// -- /api/projects/<id>/graph ---------------------------------------

export type GraphNodeType =
  | "file"
  | "external_service"
  | "spec_entity"
  | "decision_record";

export type GraphNodeStatus =
  | "pending" // declared in spec but file not yet generated
  | "writing" // worker is currently producing this file (set live by WS)
  | "complete" // ledger has a current entry
  | "failed";

export interface GraphNode {
  id: string; // artifact_key
  type: GraphNodeType;
  label: string;
  group: string; // folder for files, "services" for external, etc.
  status: GraphNodeStatus;
  data: Record<string, unknown>;
  seq: number;
}

export interface GraphEdge {
  id: string;
  source: string; // node id (= artifact_key)
  target: string;
  kind: string; // EdgeKind from the backend, e.g. "imports", "uses_service"
}

export interface GraphResponse {
  project_id: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  node_count: number;
  edge_count: number;
}

// -- /api/projects/<id>/iterate (POST) ------------------------------

export interface IterateRequest {
  prompt: string;
}

export interface IterateResponse {
  project_id: string;
  iteration_seq: number;
  status: string;
}

// -- /api/projects/<id>/iterations (GET) ----------------------------

export type IterationStatus = "running" | "complete";

export interface IterationFailure {
  path: string;
  reason: string;
}

export interface Iteration {
  seq: number;
  status: IterationStatus;
  prompt: string;
  started_at: number | null;
  completed_at: number | null;
  rationale: string;
  changes_applied: string[];
  new_files_created: string[];
  files_deleted: string[];
  failed: IterationFailure[];
  input_tokens: number;
  output_tokens: number;
  /**
   * If this iteration was triggered by the auto-patch loop (Builder
   * fixing critical findings the auditors flagged), this is the
   * autopatch attempt number (1..AUTOPATCH_MAX_ATTEMPTS, currently 3).
   * Null for user-triggered iterations.
   */
  autopatch_attempt: number | null;
}

export interface IterationListResponse {
  project_id: string;
  iterations: Iteration[];
  count: number;
}

// -- Fix-all -------------------------------------------------------

export interface FixAllEstimate {
  issues_to_fix: number;
  files_affected: number;
  truncated: boolean;
  by_severity?: {
    critical: number;
    warning: number;
    nit: number;
  };
  estimated_cost_usd_low: number;
  estimated_cost_usd_high: number;
}

export interface FixAllResponse {
  project_id: string;
  fix_all_seq: number;
  status: string;
}

export type FixAllStatus = "running" | "complete";

export interface FixAllPass {
  seq: number;
  status: FixAllStatus;
  issue_count: number;
  files_affected: number;
  started_at: number | null;
  completed_at: number | null;
  pre_count: number | null;
  post_count: number | null;
  fixed: number | null;
  regressions: number | null;
  report: string | null;
}

export interface FixAllPassesResponse {
  project_id: string;
  passes: FixAllPass[];
  count: number;
}
