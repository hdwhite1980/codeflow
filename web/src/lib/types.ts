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

export interface CreateProjectRequest {
  slug: string;
  prompt: string;
}

export interface CreateProjectResponse {
  project_id: string;
  status: string;
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
