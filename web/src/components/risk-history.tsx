"use client";

import { useState, useEffect, useCallback } from "react";
import { ChevronDown, ChevronRight, ShieldAlert } from "lucide-react";

import { getRiskHistory } from "@/lib/api";
import type { RiskAssessment } from "@/lib/types";
import { RiskResult } from "@/components/risk-analyzer";
import { formatRelativeTime } from "@/lib/format";

interface RiskHistoryProps {
  projectId: string;
  /**
   * Bumped by the parent whenever a new risk assessment lands. We
   * watch this and refetch — avoids polling.
   */
  refreshKey: number;
}

/**
 * Compact list of past risk assessments. Each row is a one-line header
 * (severity, target, change, time ago) that expands to the full
 * RiskResult panel on click.
 *
 * Empty state: a quiet placeholder line. We don't want this to look
 * like an error when the user just hasn't asked any questions yet.
 */
export function RiskHistory({ projectId, refreshKey }: RiskHistoryProps) {
  const [risks, setRisks] = useState<RiskAssessment[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [techExpanded, setTechExpanded] = useState<Set<number>>(new Set());

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await getRiskHistory(projectId);
      setRisks(resp.risks);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load risk history",
      );
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    refetch();
  }, [refetch, refreshKey]);

  function toggleExpanded(seq: number) {
    const next = new Set(expanded);
    if (next.has(seq)) next.delete(seq); else next.add(seq);
    setExpanded(next);
  }
  function toggleTech(seq: number) {
    const next = new Set(techExpanded);
    if (next.has(seq)) next.delete(seq); else next.add(seq);
    setTechExpanded(next);
  }

  if (loading && risks.length === 0) {
    return (
      <div className="text-xs text-muted-foreground p-2">
        Loading risk history…
      </div>
    );
  }
  if (error) {
    return (
      <div className="text-xs text-red-300 p-2">
        Could not load risk history: {error}
      </div>
    );
  }
  if (risks.length === 0) {
    return (
      <div className="text-xs text-muted-foreground p-2">
        No risk queries yet. Ask the guardian above to start a history.
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Past risk queries ({risks.length})
      </div>
      {risks.map((r) => {
        const isOpen = expanded.has(r.seq);
        return (
          <div
            key={r.seq}
            className="rounded-md border border-border bg-card/30"
          >
            <button
              type="button"
              onClick={() => toggleExpanded(r.seq)}
              className="w-full flex items-center gap-2 px-3 py-2 text-left text-sm hover:bg-card/60"
            >
              {isOpen
                ? <ChevronDown className="h-3 w-3 flex-shrink-0" />
                : <ChevronRight className="h-3 w-3 flex-shrink-0" />}
              <SeverityDot severity={r.severity} />
              <span className="font-mono text-xs text-muted-foreground truncate">
                {r.target}
              </span>
              <span className="truncate flex-1 min-w-0">
                {r.change_description}
              </span>
              <span className="text-xs text-muted-foreground flex-shrink-0">
                {formatRelativeTime(new Date(r.asked_at * 1000).toISOString())}
              </span>
            </button>
            {isOpen && (
              <div className="px-3 pb-3">
                <RiskResult
                  assessment={r}
                  techExpanded={techExpanded.has(r.seq)}
                  onToggleTech={() => toggleTech(r.seq)}
                />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}


function SeverityDot({ severity }: { severity: string }) {
  const color = {
    low: "bg-emerald-500",
    medium: "bg-amber-500",
    high: "bg-orange-500",
    critical: "bg-red-500",
  }[severity] ?? "bg-muted-foreground";
  return <span className={`h-2 w-2 rounded-full flex-shrink-0 ${color}`} />;
}
