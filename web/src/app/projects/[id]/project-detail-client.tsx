"use client";

import { useCallback, useMemo, useState } from "react";

import { CostSummary } from "@/components/cost-summary";
import { FileList } from "@/components/file-list";
import { LiveIndicator } from "@/components/live-indicator";
import { Badge } from "@/components/ui/badge";
import { getArtifacts, getAudits, getUsage } from "@/lib/api";
import { providerLabel } from "@/lib/format";
import type {
  ArtifactListResponse,
  ArtifactSummary,
  AuditResponse,
  UsageRow,
  UsageSummary,
  WSEvent,
} from "@/lib/types";
import { useProjectStream } from "@/lib/ws";

interface ProjectDetailClientProps {
  projectId: string;
  initialArtifacts: ArtifactListResponse | null;
  initialUsage: UsageSummary | null;
  initialAudits: AuditResponse | null;
  initialError: string | null;
}

/**
 * The live detail view.
 *
 * State machine
 * -------------
 *  1. Page loads with initial data from REST (server-rendered).
 *  2. WebSocket opens.
 *  3. ledger_entry events append to local artifacts; usage_row events
 *     append to local usage rows. We never replace via REST — that
 *     would clobber live updates.
 *  4. When a `ref:<id>:audit:outcome` ledger entry lands (build done),
 *     we refetch /audits once to get the verdict bodies (audits aren't
 *     pushed over WS, only the metadata is — that's a deliberate
 *     simplification to keep events small).
 *  5. The build is considered "complete" when both build:outcome and
 *     audit:outcome ledger entries exist. After that, the LiveIndicator
 *     shows as live but events are unlikely; user can navigate away.
 */
export function ProjectDetailClient({
  projectId,
  initialArtifacts,
  initialUsage,
  initialAudits,
  initialError,
}: ProjectDetailClientProps) {
  // We hold three live-mutable pieces of state and merge events into them.
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>(
    initialArtifacts?.artifacts ?? [],
  );
  const [usageRows, setUsageRows] = useState<UsageRow[]>(
    initialUsage?.rows ?? [],
  );
  const [audits, setAudits] = useState<AuditResponse | null>(initialAudits);
  const [lastEventAt, setLastEventAt] = useState<number | null>(null);
  const [refetching, setRefetching] = useState(false);

  // Stable handler for WS events. Reads-and-writes-only-via-setState
  // so it's safe to bind via ref in useProjectStream.
  const onEvent = useCallback(
    (event: WSEvent) => {
      setLastEventAt(Date.now());
      if (event.kind === "hello") {
        return;
      }
      if (event.kind === "ledger_entry") {
        const entry = event.data;
        setArtifacts((prev) => mergeArtifact(prev, entry));
        // If this is the audit outcome record, the build/audit pipeline
        // just finished. Refetch /audits and /artifacts to pull in the
        // full verdict bodies and any artifacts we might've missed
        // (events can drop on flaky WS — REST is authoritative).
        if (entry.artifact_key.endsWith(":audit:outcome")) {
          refetchAll();
        }
        return;
      }
      if (event.kind === "usage_row") {
        const row = event.data;
        setUsageRows((prev) => mergeUsageRow(prev, row));
        return;
      }
    },
    // refetchAll is stable below
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [],
  );

  const refetchAll = useCallback(async () => {
    if (refetching) return;
    setRefetching(true);
    try {
      const [a, u, au] = await Promise.allSettled([
        getArtifacts(projectId),
        getUsage(projectId),
        getAudits(projectId),
      ]);
      if (a.status === "fulfilled") {
        // Replace artifacts wholesale from REST; this is authoritative.
        setArtifacts(a.value.artifacts);
      }
      if (u.status === "fulfilled") {
        setUsageRows(u.value.rows);
      }
      if (au.status === "fulfilled") {
        setAudits(au.value);
      }
    } finally {
      setRefetching(false);
    }
  }, [projectId, refetching]);

  const streamState = useProjectStream(projectId, { onEvent });

  // Derive everything else from the merged state. Memoized so we don't
  // re-sort and re-rollup on every keystroke.
  const filePaths = useMemo(() => deriveFilePaths(artifacts), [artifacts]);
  const usageSummary = useMemo(
    () => summarizeUsage(projectId, usageRows),
    [projectId, usageRows],
  );
  const buildState = useMemo(
    () => deriveBuildState(artifacts),
    [artifacts],
  );

  if (initialError && artifacts.length === 0) {
    return (
      <div className="rounded-md border border-red-900/40 bg-red-950/30 p-4 text-sm text-red-300">
        Couldn&apos;t load this project: {initialError}
      </div>
    );
  }

  return (
    <div className="space-y-8">
      <div className="flex flex-wrap items-center justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">
            {buildState.title ?? projectId}
          </h1>
          <p className="mt-1 text-sm text-muted-foreground">
            {buildState.subtitle}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <BuildPhaseBadge phase={buildState.phase} />
          <LiveIndicator state={streamState} />
          {lastEventAt && (
            <span className="text-xs text-muted-foreground">
              last event {age(lastEventAt)}
            </span>
          )}
        </div>
      </div>

      <CostSummary usage={usageSummary} />

      <section className="space-y-3">
        <div className="flex items-baseline justify-between">
          <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Files
          </h2>
          {audits && (
            <div className="text-xs text-muted-foreground">
              {audits.total_findings} findings ·{" "}
              {audits.by_severity.critical} critical,{" "}
              {audits.by_severity.warning} warning,{" "}
              {audits.by_severity.nit} nit
            </div>
          )}
        </div>
        <FileList
          filePaths={filePaths}
          usageRows={usageRows}
          verdicts={audits?.verdicts ?? []}
        />
      </section>

      {audits && audits.by_auditor.length > 0 && (
        <section>
          <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
            Auditors
          </h2>
          <div className="flex flex-wrap gap-2">
            {audits.by_auditor.map((a) => (
              <div
                key={a.auditor}
                className="inline-flex items-center gap-3 rounded-md border border-border bg-card/60 px-3 py-2"
              >
                <Badge variant="provider">{providerLabel(a.auditor)}</Badge>
                <span className="text-xs text-muted-foreground">
                  {a.files_audited} files
                </span>
                <div className="flex gap-1">
                  {a.critical > 0 && (
                    <Badge variant="critical">{a.critical} critical</Badge>
                  )}
                  {a.warning > 0 && (
                    <Badge variant="warning">{a.warning} warning</Badge>
                  )}
                  {a.nit > 0 && <Badge variant="nit">{a.nit} nit</Badge>}
                </div>
              </div>
            ))}
          </div>
        </section>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// State derivation helpers — small and pure for easy reasoning.
// ---------------------------------------------------------------------------

function mergeArtifact(
  prev: ArtifactSummary[],
  incoming: ArtifactSummary,
): ArtifactSummary[] {
  // Replace by artifact_key if present (supersession); otherwise append.
  const idx = prev.findIndex((a) => a.artifact_key === incoming.artifact_key);
  if (idx === -1) return [...prev, incoming];
  const out = prev.slice();
  out[idx] = incoming;
  return out;
}

function mergeUsageRow(prev: UsageRow[], incoming: UsageRow): UsageRow[] {
  // Usage rows are append-only; no supersession. But events can be
  // duplicated by a flaky WS — dedupe by id when available.
  if (incoming.id) {
    if (prev.some((r) => r.id === incoming.id)) return prev;
  }
  return [...prev, incoming];
}

function deriveFilePaths(artifacts: ArtifactSummary[]): string[] {
  // Files appear as artifact_key = "file:<project_id>:<path>" in seq order.
  return artifacts
    .filter((a) => a.kind === "file")
    .sort((a, b) => a.seq - b.seq)
    .map((a) => {
      const parts = a.artifact_key.split(":");
      return parts.slice(2).join(":");
    });
}

function summarizeUsage(
  projectId: string,
  rows: UsageRow[],
): UsageSummary {
  // Mirror what /api/projects/<id>/usage returns server-side. We need
  // total_cost, call_count, total_tokens, by_provider rollups.
  let totalCost = 0;
  let totalTokens = 0;
  const byProvider: Record<
    string,
    {
      call_count: number;
      input_tokens: number;
      output_tokens: number;
      input_cost_usd: number;
      output_cost_usd: number;
    }
  > = {};
  for (const r of rows) {
    totalCost += r.total_cost_usd;
    totalTokens += r.input_tokens + r.output_tokens;
    const slot = (byProvider[r.provider] = byProvider[r.provider] ?? {
      call_count: 0,
      input_tokens: 0,
      output_tokens: 0,
      input_cost_usd: 0,
      output_cost_usd: 0,
    });
    slot.call_count++;
    slot.input_tokens += r.input_tokens;
    slot.output_tokens += r.output_tokens;
    slot.input_cost_usd += r.input_cost_usd;
    slot.output_cost_usd += r.output_cost_usd;
  }
  return {
    project_id: projectId,
    call_count: rows.length,
    total_tokens: totalTokens,
    total_cost_usd: totalCost,
    by_stage: [], // not used by the UI; could compute similarly
    by_provider: Object.entries(byProvider).map(([provider, v]) => ({
      stage: provider,
      call_count: v.call_count,
      input_tokens: v.input_tokens,
      output_tokens: v.output_tokens,
      input_cost_usd: v.input_cost_usd,
      output_cost_usd: v.output_cost_usd,
      total_tokens: v.input_tokens + v.output_tokens,
      total_cost_usd: v.input_cost_usd + v.output_cost_usd,
    })),
    rows,
  };
}

type BuildPhase =
  | "queued"
  | "building"
  | "auditing"
  | "complete"
  | "unknown";

function deriveBuildState(artifacts: ArtifactSummary[]): {
  phase: BuildPhase;
  title?: string;
  subtitle: string;
} {
  const keys = new Set(artifacts.map((a) => a.artifact_key.split(":").slice(-1)[0]));
  // Note: artifact_key formats vary — for `ref:<uuid>:audit:outcome` the
  // last segment is "outcome". Match by suffix instead.
  const hasBuildOutcome = artifacts.some((a) =>
    a.artifact_key.endsWith(":build:outcome"),
  );
  const hasAuditStarted = artifacts.some((a) =>
    a.artifact_key.endsWith(":audit:started"),
  );
  const hasAuditOutcome = artifacts.some((a) =>
    a.artifact_key.endsWith(":audit:outcome"),
  );
  const hasBuildStarted = artifacts.some((a) =>
    a.artifact_key.endsWith(":build:started"),
  );

  let phase: BuildPhase = "unknown";
  if (hasAuditOutcome) phase = "complete";
  else if (hasAuditStarted) phase = "auditing";
  else if (hasBuildStarted) phase = "building";
  else phase = "queued";

  const subtitle =
    phase === "complete"
      ? "Build and audit complete."
      : phase === "auditing"
        ? "Auditors are reviewing the generated files."
        : phase === "building"
          ? "Anthropic is generating files."
          : "Queued.";

  return { phase, subtitle };
}

function BuildPhaseBadge({ phase }: { phase: BuildPhase }) {
  const variant: "provider" | "warning" =
    phase === "complete" ? "provider" : "warning";
  const label =
    phase === "complete"
      ? "Complete"
      : phase === "auditing"
        ? "Auditing"
        : phase === "building"
          ? "Building"
          : phase === "queued"
            ? "Queued"
            : "—";
  return <Badge variant={variant}>{label}</Badge>;
}

function age(timestamp: number): string {
  const diff = Math.max(0, Math.round((Date.now() - timestamp) / 1000));
  if (diff < 60) return `${diff}s ago`;
  return `${Math.round(diff / 60)}m ago`;
}
