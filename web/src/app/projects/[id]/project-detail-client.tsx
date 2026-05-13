"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import { CostSummary } from "@/components/cost-summary";
import { GraphView } from "@/components/graph-view";
import { LiveIndicator } from "@/components/live-indicator";
import { Badge } from "@/components/ui/badge";
import {
  getAudits,
  getGraph,
} from "@/lib/api";
import type {
  AuditResponse,
  AuditVerdict,
  GraphResponse,
  UsageRow,
  UsageSummary,
  WSEvent,
} from "@/lib/types";
import { useProjectStream } from "@/lib/ws";

interface ProjectDetailClientProps {
  projectId: string;
  initialArtifacts: unknown; // no longer used here but passed by the page shell
  initialUsage: UsageSummary | null;
  initialAudits: AuditResponse | null;
  initialError: string | null;
}

/**
 * Project detail view. Primary content is the React Flow graph; cost
 * summary floats in the corner; live indicator at the top.
 *
 * Data flow
 * ---------
 *   1. Server pre-fetches artifacts/usage/audits in the page shell.
 *      We also fetch /graph on mount because the server can't easily
 *      proxy it through the App Router fetch cache without making
 *      everything dynamic.
 *   2. WebSocket subscribes. Each event either updates usage rows or
 *      triggers a graph refetch (debounced — see below).
 *   3. When the audit:outcome ledger entry lands, we refetch /audits
 *      to pull verdict bodies.
 *
 * Refetch policy
 * --------------
 * We refetch /graph on every ledger_entry event because nodes/edges can
 * change in non-obvious ways (new file → new graph_edges from imports).
 * REST is authoritative; the WebSocket is a tap on the firehose telling
 * us "something changed, look again." The fetches are cheap (the graph
 * endpoint is just a couple of queries) and avoid stale-state bugs we'd
 * have to debug otherwise.
 */
export function ProjectDetailClient({
  projectId,
  initialAudits,
  initialUsage,
  initialError,
}: ProjectDetailClientProps) {
  const [graph, setGraph] = useState<GraphResponse | null>(null);
  const [usageRows, setUsageRows] = useState<UsageRow[]>(
    initialUsage?.rows ?? [],
  );
  const [audits, setAudits] = useState<AuditResponse | null>(initialAudits);
  const [refetchPending, setRefetchPending] = useState(false);

  // Initial graph fetch on mount.
  useEffect(() => {
    let cancelled = false;
    getGraph(projectId)
      .then((g) => {
        if (!cancelled) setGraph(g);
      })
      .catch((err) => {
        console.error("[detail] initial graph fetch failed:", err);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // Refetch graph + audits. Called when WS events suggest the
  // underlying ledger has changed in ways our local state can't fully
  // reconcile. Coalesced so a burst of events doesn't cause a refetch
  // per event.
  const refetchGraphAndAudits = useCallback(async () => {
    if (refetchPending) return;
    setRefetchPending(true);
    try {
      const [g, au] = await Promise.allSettled([
        getGraph(projectId),
        getAudits(projectId),
      ]);
      if (g.status === "fulfilled") setGraph(g.value);
      if (au.status === "fulfilled") setAudits(au.value);
    } finally {
      setRefetchPending(false);
    }
  }, [projectId, refetchPending]);

  // Debounce graph refetches across rapid WS bursts (e.g. multiple
  // ledger_entry events arriving within ~200ms during the file
  // generation phase). The simplest debounce that survives React's
  // strict-mode double-mount: a module-level timer in useRef would be
  // overkill; just use a state flag combined with the inflight guard.
  const [refetchDebounceTimer, setRefetchDebounceTimer] = useState<
    ReturnType<typeof setTimeout> | null
  >(null);
  const scheduleRefetch = useCallback(() => {
    if (refetchDebounceTimer) clearTimeout(refetchDebounceTimer);
    const t = setTimeout(() => {
      refetchGraphAndAudits();
      setRefetchDebounceTimer(null);
    }, 250);
    setRefetchDebounceTimer(t);
  }, [refetchDebounceTimer, refetchGraphAndAudits]);

  // WS event handler.
  const onEvent = useCallback(
    (event: WSEvent) => {
      if (event.kind === "hello") return;
      if (event.kind === "ledger_entry") {
        // Any ledger write could have added/changed graph nodes or edges.
        // Refetch.
        scheduleRefetch();
        return;
      }
      if (event.kind === "usage_row") {
        const row = event.data;
        setUsageRows((prev) => mergeUsageRow(prev, row));
        return;
      }
    },
    [scheduleRefetch],
  );

  const streamState = useProjectStream(projectId, { onEvent });

  // Build the per-file verdict map once per audits update — used by
  // both the graph nodes (finding count pills) and the side panel.
  const verdictsByPath = useMemo<Map<string, AuditVerdict[]>>(() => {
    const m = new Map<string, AuditVerdict[]>();
    if (!audits) return m;
    for (const v of audits.verdicts) {
      const list = m.get(v.file_path) ?? [];
      list.push(v);
      m.set(v.file_path, list);
    }
    return m;
  }, [audits]);

  // Summarize usage live from local rows.
  const usageSummary = useMemo(
    () => summarizeUsage(projectId, usageRows),
    [projectId, usageRows],
  );

  if (initialError && !graph) {
    return (
      <div className="rounded-md border border-red-900/40 bg-red-950/30 p-4 text-sm text-red-300">
        Couldn&apos;t load this project: {initialError}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-lg font-semibold tracking-tight">
            {projectId.slice(0, 8)}…
          </h1>
          <p className="text-xs text-muted-foreground">
            Live build view · click any node for details
          </p>
        </div>
        <div className="flex items-center gap-3">
          <BuildPhaseBadge graph={graph} audits={audits} />
          <LiveIndicator state={streamState} />
        </div>
      </header>

      <GraphView graph={graph} verdictsByPath={verdictsByPath} />

      {/* Cost ticker pinned to the bottom-left of the viewport, sized
          modestly so it doesn't compete with the graph for attention.
          Bottom-left avoids collision with the side panel (which slides
          in from the right). */}
      <div className="pointer-events-none fixed bottom-6 left-6 z-30 w-72">
        <div className="pointer-events-auto">
          <CostSummary usage={usageSummary} />
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Phase derivation — best-effort label of where the build is in its
// lifecycle. Used by the header badge so the user can tell at a glance
// whether things are still running.
// ---------------------------------------------------------------------------

function BuildPhaseBadge({
  graph,
  audits,
}: {
  graph: GraphResponse | null;
  audits: AuditResponse | null;
}) {
  // Lightweight inference. If we have any non-pending file nodes,
  // we're past the spec phase. If audits has verdicts, audit has run.
  const hasFiles = graph?.nodes.some(
    (n) => n.type === "file" && n.status === "complete",
  );
  const hasPending = graph?.nodes.some(
    (n) => n.type === "file" && n.status === "pending",
  );
  const hasAudits = (audits?.verdict_count ?? 0) > 0;

  let label = "queued";
  let variant: "warning" | "provider" = "warning";
  if (hasAudits) {
    label = "complete";
    variant = "provider";
  } else if (hasFiles && hasPending) {
    label = "building";
  } else if (hasFiles && !hasPending) {
    label = "auditing";
  } else if (hasPending) {
    label = "planning";
  }

  return <Badge variant={variant}>{label}</Badge>;
}

// ---------------------------------------------------------------------------
// Local state helpers (carried over from the dashboard version).
// ---------------------------------------------------------------------------

function mergeUsageRow(prev: UsageRow[], incoming: UsageRow): UsageRow[] {
  if (incoming.id) {
    if (prev.some((r) => r.id === incoming.id)) return prev;
  }
  return [...prev, incoming];
}

function summarizeUsage(projectId: string, rows: UsageRow[]): UsageSummary {
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
    by_stage: [],
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
