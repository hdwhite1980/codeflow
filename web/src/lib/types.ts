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

// -- Guardian risk analyzer ----------------------------------------

export type RiskSeverity = "low" | "medium" | "high" | "critical";

export interface RiskConcern {
  path: string;
  reason: string;
  severity: RiskSeverity;
}

export interface RiskQueryRequest {
  target: string;
  change_description: string;
}

export interface RiskAssessment {
  seq: number;
  project_id: string;
  target: string;
  change_description: string;
  severity: RiskSeverity;
  plain_narrative: string;
  technical_narrative: string;
  affected_paths: string[];
  concerns: RiskConcern[];
  suggested_sequencing: string[];
  confidence: number;
  analyzer_model: string;
  indexed_summary_count: number;
  indexed_symbol_count?: number;
  asked_at: number;
}

export interface RiskListResponse {
  project_id: string;
  risks: RiskAssessment[];
  count: number;
}

// -- Iteration / fix-all attached risk records (Turn D.1 + D.2) ----
//
// The risk records written by run_iteration() and handle_fix_all()
// have the SAME body shape as RiskAssessment but without the
// project_id/seq fields wrapped at the top (those are implied by the
// containing iteration/fix-all). The frontend treats them as
// "AttachedRiskAssessment" — same fields the user already understands
// from the standalone risk panel, just inline in an iteration card.

export interface AttachedRiskAssessment {
  target: string;
  change_description: string;
  severity: RiskSeverity;
  plain_narrative: string;
  technical_narrative: string;
  affected_paths: string[];
  concerns: RiskConcern[];
  suggested_sequencing: string[];
  confidence: number;
  analyzer_model: string;
  indexed_summary_count: number;
  indexed_symbol_count?: number;
  asked_at: number;
}

export interface IterationCancellation {
  iteration_seq: number;
  reason: string;            // "cancel" | "timeout"
  pre_risk_severity: string;
  cancelled_at: number;
}

export interface FixAllCancellation {
  fix_all_seq: number;
  reason: string;
  pre_risk_severity: string;
  cancelled_at: number;
}

// One iteration's set of risk records. All three fields are optional;
// when an iteration is paused at pre-flight, only `pre_risk` is set.
export interface IterationRisks {
  pre_risk?: AttachedRiskAssessment;
  post_risk?: AttachedRiskAssessment;
  cancelled?: IterationCancellation;
}

export interface FixAllRisks {
  pre_risk?: AttachedRiskAssessment;
  post_risk?: AttachedRiskAssessment;
  cancelled?: FixAllCancellation;
}

// API response shape: keyed by iteration seq (number).
export interface IterationRisksResponse {
  project_id: string;
  by_seq: Record<number, IterationRisks>;
}

export interface FixAllRisksResponse {
  project_id: string;
  by_seq: Record<number, FixAllRisks>;
}

// Proceed/cancel decision response from POST endpoints.
export interface RiskDecisionResponse {
  project_id: string;
  kind: "iteration" | "fix_all";
  seq: number;
  decision: "proceed" | "cancel";
  notified: boolean;
}

// -- Memory visibility (Turn G-A) ---------------------------------
//
// Each iteration that ran with guardian summaries available writes a
// memory_references record listing which files were pulled into context.
// The frontend uses this to surface "guardian referenced N files" on
// every iteration card, with the file list one click away.

export interface MemoryReferences {
  iteration_seq: number;
  referenced_paths: string[];
  reference_count: number;
  context_chars: number;
}

export interface MemoryReferencesResponse {
  project_id: string;
  by_seq: Record<number, MemoryReferences>;
}

// -- Project Memory panel (Turn G-B) -------------------------------
//
// One entry per guardian-indexed file. The data is the same FileSummary
// shape the worker writes, plus cross-references to risk queries that
// mentioned this file — so the panel can show "auth.py was the target
// of risk query #3 and a concern in #7" without an extra fetch.

export interface ProjectMemoryEntry {
  file_path: string;
  plain_english: string;
  technical: string;
  purpose: string;
  touches: string[];
  assumes: string[];
  failure_modes: string[];
  risk_notes: string[];
  indexed_at: number;
  indexer_model: string;
  input_tokens: number;
  output_tokens: number;
  risk_queries_as_target: number[];
  risk_queries_as_concern: number[];
  // Set by the backend when the file's ledger entry is newer than the
  // summary's indexed_at (with a 30s grace). UI surfaces this as a
  // "stale" pill so users know to re-index.
  is_stale?: boolean;
}

export interface ProjectMemoryResponse {
  project_id: string;
  summaries: ProjectMemoryEntry[];
  count: number;
}

// -- Import existing repo (Turn G-C) -------------------------------

export interface ImportProjectRequest {
  url: string;
  slug?: string;
}

export interface ImportProjectResponse {
  project_id: string;
  slug: string;
  status: string;
}

// -- Symbol-grained memory (Turn B) -------------------------------
//
// Per-symbol semantic summary. Same kind/shape as ProjectMemoryEntry
// but scoped to one function/class/method/constant inside a file
// rather than the whole file. Backend persists these under artifact
// keys `semantic_symbol:<pid>:<path>:<kind>:<qualified_name>`.

export interface SymbolSummary {
  file_path: string;
  symbol_kind: string;      // function | method | class | constant | struct | enum | ...
  symbol_name: string;      // short name, e.g. "verify_password"
  qualified_name: string;   // e.g. "AuthService.verify_password"
  start_line: number;
  end_line: number;
  plain_english: string;
  purpose: string;
  touches: string[];
  assumes: string[];
  failure_modes: string[];
  risk_notes: string[];
  indexed_at: number;
  indexer_model: string;
  input_tokens: number;
  output_tokens: number;
}

export interface ProjectMemorySymbolsResponse {
  project_id: string;
  file_path: string | null;
  symbols: SymbolSummary[];
  count: number;
}

// -- Ambient findings (Turn E) ------------------------------------
//
// Project-level concerns generated automatically from guardian
// summaries' risk_notes. The backend runs ambient review after every
// guardian_index pass and persists findings to the ledger; the
// frontend renders them in the AmbientFindingsPanel.

export interface AmbientFindingEvidence {
  path: string;
  note: string;
  score: number;
}

export interface AmbientFinding {
  digest: string;        // stable hash; used as the dismiss path id
  severity: "low" | "medium" | "high" | "critical";
  score: number;
  title: string;
  description: string;
  file_paths: string[];
  evidence: AmbientFindingEvidence[];
  detected_at: number;
  dismissed_at?: number | null;
  dismissed_reason?: string | null;
}

export interface AmbientFindingsResponse {
  project_id: string;
  findings: AmbientFinding[];
  count: number;
}

// -- Auditor disagreements (Fix C UI) -----------------------------
//
// When fix-all detects that OpenAI and Gemini disagree on a
// finding (same file/line region, contradictory suggestions), it
// surfaces the group here instead of auto-fixing either side.
// The user picks which auditor was right (or dismisses both)
// via /disagreements/<digest>/resolve.

export interface DisagreementFinding {
  auditor: string;
  severity: string;
  line: number | null;
  issue: string;
  suggestion: string;
}

export interface AuditorDisagreement {
  digest: string;
  seq: number;
  file_path: string;
  line: number | null;
  findings: DisagreementFinding[];
  detected_at: number;
  resolved_at?: number | null;
  resolved_auditor?: string | null;
  resolved_action?: string | null;  // "queue_fix" | "dismiss_both"
}

export interface DisagreementsResponse {
  project_id: string;
  disagreements: AuditorDisagreement[];
  count: number;
}

export interface ResolveDisagreementResponse {
  project_id: string;
  digest: string;
  resolved: boolean;
  action: string;
  queued_finding?: DisagreementFinding | null;
}

// -- Decisions needed (Fix D + Fix F UI) --------------------------
//
// When the Builder declines to fix a finding because it requires
// human input (URL, architectural commitment, exception contract),
// fix-all persists the refusal as a decision_needed record. Users
// see them in the DecisionsNeededPanel and resolve them by
// providing a value or dismissing the finding.

export interface DecisionNeeded {
  digest: string;
  seq: number;
  file_path: string;
  line: number | null;
  issue: string;
  decision_needed: string;
  decision_type: "value" | "architectural" | "contract" | "policy";
  blocking_info: string;
  detected_at: number;
  resolved_at?: number | null;
  resolved_value?: string | null;
  resolved_action?: string | null;  // "provide_value" | "dismiss"
}

export interface DecisionsResponse {
  project_id: string;
  decisions: DecisionNeeded[];
  count: number;
}

export interface ResolveDecisionResponse {
  project_id: string;
  digest: string;
  resolved: boolean;
  action: string;
}
