"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { Download } from "lucide-react";

import { CostSummary } from "@/components/cost-summary";
import { FixAllButton } from "@/components/fix-all-button";
import { FixAllHistory } from "@/components/fix-all-history";
import { GraphView } from "@/components/graph-view";
import { IterationHistory } from "@/components/iteration-history";
import { IterationInput } from "@/components/iteration-input";
import { LiveIndicator } from "@/components/live-indicator";
import { RiskAnalyzer } from "@/components/risk-analyzer";
import { RiskHistory } from "@/components/risk-history";
import { Badge } from "@/components/ui/badge";
import {
  getAudits,
  getFixAllPasses,
  getFixAllRisks,
  getGraph,
  getIterationRisks,
  getIterations,
  getMemoryReferences,
} from "@/lib/api";
import type {
  AuditResponse,
  AuditVerdict,
  FixAllPass,
  FixAllRisks,
  GraphResponse,
  Iteration,
  IterationRisks,
  MemoryReferences,
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
  const [iterations, setIterations] = useState<Iteration[]>([]);
  // Iteration sequence numbers we've queued or know are running, but
  // haven't yet seen complete in the iterations list. These let the
  // UI show "running" state immediately on queue, before the next
  // /api/projects/<id>/iterations poll lands.
  const [inFlightSeqs, setInFlightSeqs] = useState<Set<number>>(new Set());
  const [fixAllPasses, setFixAllPasses] = useState<FixAllPass[]>([]);
  // True if a fix-all pass is in flight (either queued just now or
  // still running on the worker). Derived from the passes list plus
  // the most recent triggered seq.
  const [fixAllInFlightSeqs, setFixAllInFlightSeqs] = useState<Set<number>>(
    new Set(),
  );
  const [refetchPending, setRefetchPending] = useState(false);
  // Bumped every time a new risk assessment lands so RiskHistory refetches.
  // We can't use the WebSocket for this because risk queries are sync
  // (no ledger_entry event flows through the event bus in time).
  const [riskRefreshKey, setRiskRefreshKey] = useState(0);

  // Iteration/fix-all attached risk records, keyed by seq. Fetched
  // separately from iterations themselves since they live in the
  // DECISION_RECORD ledger entries and are written by the worker as
  // the iteration progresses (pre_risk → regen → post_risk).
  const [iterationRisks, setIterationRisks] = useState<
    Record<number, IterationRisks>
  >({});
  const [fixAllRisks, setFixAllRisks] = useState<
    Record<number, FixAllRisks>
  >({});
  // Memory references keyed by iteration seq — which guardian files
  // were pulled into context for each iteration. Surfaced in the
  // iteration history cards as a clickable "Guardian referenced N
  // files" badge.
  const [memoryReferences, setMemoryReferences] = useState<
    Record<number, MemoryReferences>
  >({});

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

  // Initial iterations fetch (separate from graph so each can fail
  // independently — a missing /iterations endpoint shouldn't break the
  // graph view).
  useEffect(() => {
    let cancelled = false;
    getIterations(projectId)
      .then((res) => {
        if (!cancelled) setIterations(res.iterations);
      })
      .catch((err) => {
        console.error("[detail] initial iterations fetch failed:", err);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // Initial fix-all passes fetch.
  useEffect(() => {
    let cancelled = false;
    getFixAllPasses(projectId)
      .then((res) => {
        if (!cancelled) setFixAllPasses(res.passes);
      })
      .catch((err) => {
        console.error("[detail] initial fix-all passes fetch failed:", err);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // Initial fetch of iteration + fix-all risk records (Turn D.2)
  // plus per-iteration memory references (Turn G-A).
  // These are independent of the iterations/passes fetches above because
  // they live in DECISION_RECORD entries. We fetch them once on mount;
  // subsequent updates come through the refetch path.
  useEffect(() => {
    let cancelled = false;
    Promise.allSettled([
      getIterationRisks(projectId),
      getFixAllRisks(projectId),
      getMemoryReferences(projectId),
    ]).then(([ir, fr, mr]) => {
      if (cancelled) return;
      if (ir.status === "fulfilled") {
        setIterationRisks(ir.value.by_seq ?? {});
      }
      if (fr.status === "fulfilled") {
        setFixAllRisks(fr.value.by_seq ?? {});
      }
      if (mr.status === "fulfilled") {
        setMemoryReferences(mr.value.by_seq ?? {});
      }
    });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // Refetch graph + audits + iterations + fix-all passes. Called when
  // WS events suggest the underlying ledger has changed in ways our
  // local state can't fully reconcile. Coalesced so a burst of events
  // doesn't cause a refetch per event.
  //
  // Defensive merge: we never replace a populated graph with one that
  // has FEWER file nodes. This is a hedge against an apparent issue
  // where /graph briefly returns sparse responses during heavy ledger
  // writes (e.g. an active audit pass), which would otherwise blank
  // the canvas. We prefer the union of what we knew before and what
  // the server just told us — REST is authoritative for new nodes and
  // status updates, but our local state isn't allowed to forget nodes
  // that were there a moment ago.
  const refetchGraphAndAudits = useCallback(async () => {
    if (refetchPending) return;
    setRefetchPending(true);
    try {
      const [g, au, it, fa, ir, fr, mr] = await Promise.allSettled([
        getGraph(projectId),
        getAudits(projectId),
        getIterations(projectId),
        getFixAllPasses(projectId),
        getIterationRisks(projectId),
        getFixAllRisks(projectId),
        getMemoryReferences(projectId),
      ]);
      if (g.status === "fulfilled") {
        setGraph((prev) => mergeGraphPreservingNodes(prev, g.value));
      }
      if (au.status === "fulfilled") setAudits(au.value);
      if (it.status === "fulfilled") {
        setIterations(it.value.iterations);
        // Drop any in-flight seqs that now appear as complete.
        setInFlightSeqs((prev) => {
          const completedSeqs = new Set(
            it.value.iterations
              .filter((x) => x.status === "complete")
              .map((x) => x.seq),
          );
          const remaining = new Set<number>();
          for (const seq of prev) {
            if (!completedSeqs.has(seq)) remaining.add(seq);
          }
          return remaining;
        });
      }
      if (fa.status === "fulfilled") {
        setFixAllPasses(fa.value.passes);
        // Drop fix-all in-flight seqs whose passes are now complete.
        setFixAllInFlightSeqs((prev) => {
          const completed = new Set(
            fa.value.passes
              .filter((x) => x.status === "complete")
              .map((x) => x.seq),
          );
          const remaining = new Set<number>();
          for (const seq of prev) {
            if (!completed.has(seq)) remaining.add(seq);
          }
          return remaining;
        });
      }
      // Risk records — keyed by seq, replace wholesale (no merging needed
      // because the records are immutable once written).
      if (ir.status === "fulfilled") {
        setIterationRisks(ir.value.by_seq ?? {});
      }
      if (fr.status === "fulfilled") {
        setFixAllRisks(fr.value.by_seq ?? {});
      }
      if (mr.status === "fulfilled") {
        setMemoryReferences(mr.value.by_seq ?? {});
      }
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
  //
  // Two layers of update:
  //   1. Optimistic: immediately mutate local graph state when an event
  //      tells us something specific (a usage_row for stage=file means
  //      "the worker is generating this file right now" → mark writing;
  //      a file: ledger_entry means "the file just landed" → mark
  //      complete).
  //   2. Background: schedule a debounced refetch of /graph and /audits
  //      so any drift between local optimistic state and the source of
  //      truth gets reconciled within ~250ms.
  //
  // The optimistic layer is what makes the build "light up" visually
  // — file nodes pulse blue when the worker starts on them, then
  // settle to solid green as the ledger entry lands.
  const onEvent = useCallback(
    (event: WSEvent) => {
      if (event.kind === "hello") return;

      if (event.kind === "usage_row") {
        const row = event.data;
        setUsageRows((prev) => mergeUsageRow(prev, row));
        // If the worker is recording a per-file generation call,
        // mark that file node as 'writing' so it pulses. The ledger
        // entry will land shortly after and we'll flip it to 'complete'.
        if (row.stage === "file" && row.subject) {
          setGraph((prev) =>
            prev ? applyWritingStatus(prev, projectId, row.subject!) : prev,
          );
        }
        return;
      }

      if (event.kind === "ledger_entry") {
        const entry = event.data;
        // File entry means the file just landed in the ledger. Flip
        // any matching node from pending/writing to complete and append
        // if we don't have it yet.
        if (entry.kind === "file") {
          setGraph((prev) =>
            prev ? applyFileLanded(prev, entry.artifact_key) : prev,
          );
        }
        // Risk-related ledger entries (Turn D.1 backend wrote these):
        //   iteration:<seq>:pre_risk
        //   iteration:<seq>:post_risk
        //   iteration:<seq>:cancelled
        //   fix_all:<seq>:pre_risk
        //   fix_all:<seq>:post_risk
        //   fix_all:<seq>:cancelled
        //   guardian:risk:<seq>     (standalone risk panel)
        // We don't strictly need special handling here because
        // scheduleRefetch() below pulls the risks endpoints too, but
        // bumping the standalone risk panel's refresh key keeps it in
        // sync when a guardian:risk:* entry arrives via WS.
        if (
          entry.artifact_key?.startsWith("guardian:risk:") ||
          entry.artifact_key?.startsWith("iteration:") ||
          entry.artifact_key?.startsWith("fix_all:")
        ) {
          setRiskRefreshKey((k) => k + 1);
        }
        // Service entries can land at create time; refetch picks them
        // up. Any entry type could mean new edges; defer to refetch.
        scheduleRefetch();
        return;
      }
    },
    [projectId, scheduleRefetch],
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

  // Called by IterationInput when the user submits a new iteration.
  // We seed two pieces of optimistic state:
  //   1. inFlightSeqs gets the new seq so the history shows "running"
  //   2. A placeholder Iteration row is prepended so the user sees it
  //      immediately rather than waiting for the next refetch.
  // The next /iterations refetch will overwrite our placeholder with
  // the real entry once :started lands.
  const onIterationQueued = useCallback(
    (seq: number, prompt: string) => {
      setInFlightSeqs((prev) => {
        const next = new Set(prev);
        next.add(seq);
        return next;
      });
      const placeholder: Iteration = {
        seq,
        status: "running",
        prompt,
        started_at: Date.now() / 1000,
        completed_at: null,
        rationale: "Queued…",
        changes_applied: [],
        new_files_created: [],
        files_deleted: [],
        failed: [],
        input_tokens: 0,
        output_tokens: 0,
        // User-triggered iterations are not autopatch.
        autopatch_attempt: null,
      };
      setIterations((prev) => {
        // Replace if we already have a row for this seq (defensive), else prepend.
        const idx = prev.findIndex((it) => it.seq === seq);
        if (idx === -1) return [placeholder, ...prev];
        const out = [...prev];
        out[idx] = placeholder;
        return out;
      });
      // Also schedule a refetch — the worker should write :started
      // within a few seconds, and we want the rationale field to
      // reflect that instead of staying "Queued…".
      scheduleRefetch();
    },
    [scheduleRefetch],
  );

  // Fix-all trigger handler. Mirrors onIterationQueued: optimistic
  // marker + scheduled refetch so the user sees feedback immediately.
  const onFixAllTriggered = useCallback(
    (seq: number) => {
      setFixAllInFlightSeqs((prev) => {
        const next = new Set(prev);
        next.add(seq);
        return next;
      });
      const placeholder: FixAllPass = {
        seq,
        status: "running",
        issue_count: 0,
        files_affected: 0,
        started_at: Date.now() / 1000,
        completed_at: null,
        pre_count: null,
        post_count: null,
        fixed: null,
        regressions: null,
        report: null,
      };
      setFixAllPasses((prev) => {
        const idx = prev.findIndex((p) => p.seq === seq);
        if (idx === -1) return [placeholder, ...prev];
        const out = [...prev];
        out[idx] = placeholder;
        return out;
      });
      scheduleRefetch();
    },
    [scheduleRefetch],
  );

  if (initialError && !graph) {
    return (
      <div className="rounded-md border border-red-900/40 bg-red-950/30 p-4 text-sm text-red-300">
        Couldn&apos;t load this project: {initialError}
      </div>
    );
  }

  const anyIterationRunning = inFlightSeqs.size > 0
    || iterations.some((it) => it.status === "running");
  const fixAllRunning = fixAllInFlightSeqs.size > 0
    || fixAllPasses.some((p) => p.status === "running");

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
          <FixAllButton
            projectId={projectId}
            onTriggered={onFixAllTriggered}
            inFlight={fixAllRunning}
          />
          <a
            href={`${process.env.NEXT_PUBLIC_API_URL?.replace(/\/$/, "")}/api/projects/${encodeURIComponent(projectId)}/download`}
            download
            className="inline-flex items-center gap-1 rounded-md border border-border bg-card px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-zinc-600 hover:text-foreground"
            title="Download all files as a ZIP"
          >
            <Download className="h-3 w-3" />
            Download
          </a>
        </div>
      </header>

      <GraphView graph={graph} verdictsByPath={verdictsByPath} />

      {/* Iteration history pinned to the right edge above the side panel.
          Fix-all passes appear above iterations because they're "bigger"
          events (more cost, more scope, more user attention warranted). */}
      <div className="pointer-events-none fixed right-6 top-20 z-20 w-80">
        <div className="pointer-events-auto max-h-[60vh] space-y-3 overflow-y-auto">
          <FixAllHistory
            passes={fixAllPasses}
            risksBySeq={fixAllRisks}
            projectId={projectId}
            onDecisionMade={() => scheduleRefetch()}
          />
          <IterationHistory
            iterations={iterations}
            inFlightSeqs={inFlightSeqs}
            risksBySeq={iterationRisks}
            memoryBySeq={memoryReferences}
            projectId={projectId}
            onDecisionMade={() => scheduleRefetch()}
          />
        </div>
      </div>

      {/* Guardian risk panel — left-edge slide-out below the cost ticker.
          Lives separately from iteration/fix-all because risk queries are
          a different kind of action (read-only reasoning, not generation).
          We collapse the analyzer when not in use so the cost ticker
          stays prominent. */}
      <div className="pointer-events-none fixed left-6 top-20 z-20 w-96">
        <div className="pointer-events-auto max-h-[60vh] space-y-3 overflow-y-auto">
          <RiskAnalyzer
            projectId={projectId}
            onAssessment={() => setRiskRefreshKey((k) => k + 1)}
          />
          <RiskHistory projectId={projectId} refreshKey={riskRefreshKey} />
        </div>
      </div>

      {/* Cost ticker pinned to the bottom-left of the viewport. */}
      <div className="pointer-events-none fixed bottom-6 left-6 z-30 w-72">
        <div className="pointer-events-auto">
          <CostSummary usage={usageSummary} />
        </div>
      </div>

      {/* Iteration prompt input pinned to the bottom-center.
          Width matches the graph canvas; the cost card on the left
          and (eventually) side panel on the right give it room. */}
      <div className="pointer-events-none fixed bottom-6 left-1/2 z-30 w-[480px] -translate-x-1/2">
        <IterationInput
          projectId={projectId}
          onIterationQueued={onIterationQueued}
          someIterationRunning={anyIterationRunning}
        />
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
// Graph merge — preserves locally-known nodes when a refetch returns
// a sparser response. Why we need this: the /graph endpoint reads
// from `current_artifacts` and during heavy ledger write activity
// (e.g. an audit pass writing dozens of verdict entries per second)
// we've observed occasional sparse responses that would blank the
// canvas. Treating each refetch as a "fill in what's new" instead of
// a wholesale replace makes the UI robust to that.
//
// Rules:
//   - Service nodes: server is authoritative (these don't change after
//     project creation in normal use).
//   - File nodes: union by id. New server status wins for known ids.
//     Local-only ids (added optimistically via WS) survive even if
//     the server doesn't know about them yet.
//   - Edges: server is authoritative. Edges come from import extraction
//     which is server-side; we can't reconstruct them locally anyway.
//
// This makes setGraph an upsert rather than a replace for nodes, but
// keeps edges as a clean replace.
// ---------------------------------------------------------------------------

function mergeGraphPreservingNodes(
  prev: GraphResponse | null,
  next: GraphResponse,
): GraphResponse {
  if (!prev) return next;
  const nextById = new Map(next.nodes.map((n) => [n.id, n]));
  const mergedNodes = [...next.nodes];
  // Append any prev nodes the server forgot about.
  for (const p of prev.nodes) {
    if (!nextById.has(p.id)) {
      mergedNodes.push(p);
    }
  }
  return {
    ...next,
    nodes: mergedNodes,
    node_count: mergedNodes.length,
  };
}

// ---------------------------------------------------------------------------

/**
 * Find a file node by its path and flip its status to 'writing'.
 *
 * The path comes from a usage_row.subject — we have to construct the
 * artifact_key to match against the graph's node ids. If no matching
 * node exists yet (the spec entry hasn't been seen), we insert a
 * placeholder so the user sees the file appear immediately.
 */
function applyWritingStatus(
  graph: GraphResponse,
  projectId: string,
  filePath: string,
): GraphResponse {
  const expectedKey = `file:${projectId}:${filePath}`;
  const existing = graph.nodes.find((n) => n.id === expectedKey);
  if (existing) {
    if (existing.status === "complete") {
      // Already done; don't downgrade. This handles out-of-order events
      // (a usage_row arriving after its matching ledger_entry).
      return graph;
    }
    return {
      ...graph,
      nodes: graph.nodes.map((n) =>
        n.id === expectedKey ? { ...n, status: "writing" } : n,
      ),
    };
  }
  // Insert a new writing node. group is best-guess from the path.
  return {
    ...graph,
    nodes: [
      ...graph.nodes,
      {
        id: expectedKey,
        type: "file",
        label: filePath,
        group: filePath.includes("/")
          ? filePath.slice(0, filePath.lastIndexOf("/"))
          : "root",
        status: "writing",
        data: { path: filePath, rationale: "Generation in progress." },
        seq: -1,
      },
    ],
    node_count: graph.node_count + 1,
  };
}

/**
 * A `file:` ledger entry just landed — flip the matching node to
 * 'complete'. If no node exists yet (unusual but possible), append.
 *
 * Edges (imports, binds_env_var) come from the same ledger write so
 * the background refetch will pick them up. We don't try to derive
 * edges optimistically — the import extractor runs on the worker
 * side and we don't have its output here.
 */
function applyFileLanded(
  graph: GraphResponse,
  artifactKey: string,
): GraphResponse {
  const existing = graph.nodes.find((n) => n.id === artifactKey);
  if (existing) {
    return {
      ...graph,
      nodes: graph.nodes.map((n) =>
        n.id === artifactKey ? { ...n, status: "complete" } : n,
      ),
    };
  }
  // Derive path and group from the artifact key.
  // Key shape: "file:<project_id>:<path>"
  const parts = artifactKey.split(":");
  if (parts.length < 3) return graph;
  const path = parts.slice(2).join(":");
  const group = path.includes("/")
    ? path.slice(0, path.lastIndexOf("/"))
    : "root";
  return {
    ...graph,
    nodes: [
      ...graph.nodes,
      {
        id: artifactKey,
        type: "file",
        label: path,
        group,
        status: "complete",
        data: { path, rationale: "" },
        seq: 0,
      },
    ],
    node_count: graph.node_count + 1,
  };
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
