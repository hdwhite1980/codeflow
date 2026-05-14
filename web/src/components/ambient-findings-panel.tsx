"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertOctagon,
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Info,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { dismissFinding, getAmbientFindings } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { AmbientFinding } from "@/lib/types";

interface AmbientFindingsPanelProps {
  projectId: string;
  /**
   * Bumped by the parent when guardian indexing completes or any
   * ambient_finding:* ledger entry arrives via WebSocket. Triggers
   * a refetch so users see new findings appear automatically.
   */
  refreshKey?: number;
}

/**
 * Project Concerns panel — the user-facing surface for Turn E
 * ambient review.
 *
 * Renders findings auto-generated from guardian risk_notes. Severity
 * color-coded (critical = red, high = orange, medium = amber, low =
 * muted). Click a finding to expand and see the per-file evidence
 * that produced it. Each row has a small × to dismiss (with optional
 * reason via a tiny inline prompt).
 *
 * Dismissed findings disappear from the panel but persist in the
 * ledger. If the same digest re-emerges with stronger evidence the
 * backend reactivates it and the panel shows it again.
 *
 * Empty state distinguishes two cases:
 *   - No findings yet → "Guardian hasn't flagged anything. Either
 *     this codebase is clean, or indexing hasn't run yet."
 *   - Indexing in progress → user will see findings stream in via
 *     WS-driven refetch.
 */
export function AmbientFindingsPanel({
  projectId, refreshKey = 0,
}: AmbientFindingsPanelProps) {
  const [findings, setFindings] = useState<AmbientFinding[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await getAmbientFindings(projectId);
      setFindings(resp.findings);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load findings",
      );
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    refetch();
  }, [refetch, refreshKey]);

  async function handleDismiss(digest: string) {
    // Optimistic: remove from local state immediately, then call API.
    // If the call fails, refetch puts it back.
    setFindings((prev) => prev.filter((f) => f.digest !== digest));
    try {
      await dismissFinding(projectId, digest);
    } catch (err) {
      console.error("[ambient] dismiss failed:", err);
      refetch();
    }
  }

  const criticalCount = findings.filter(
    (f) => f.severity === "critical",
  ).length;
  const highCount = findings.filter((f) => f.severity === "high").length;

  return (
    <div className="rounded-md border border-border bg-card/50 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <AlertOctagon className="h-4 w-4 text-amber-400" />
        <h3 className="text-sm font-semibold tracking-tight">
          Project Concerns
        </h3>
        <span className="text-xs text-muted-foreground tabular-nums">
          {findings.length}
          {criticalCount > 0 && (
            <span className="ml-2 text-red-300">
              · {criticalCount} critical
            </span>
          )}
          {criticalCount === 0 && highCount > 0 && (
            <span className="ml-2 text-orange-300">
              · {highCount} high
            </span>
          )}
        </span>
        <Button
          variant="ghost"
          size="sm"
          onClick={refetch}
          disabled={loading}
          className="ml-auto h-6 w-6 p-0"
          title="Refresh"
        >
          <RefreshCw className={cn("h-3 w-3", loading && "animate-spin")} />
        </Button>
      </div>

      {error && (
        <div className="rounded border border-red-900/40 bg-red-950/30 p-2 text-xs text-red-300">
          {error}
        </div>
      )}

      {loading && findings.length === 0 && (
        <div className="text-xs text-muted-foreground">
          Loading concerns…
        </div>
      )}

      {!loading && findings.length === 0 && !error && (
        <div className="text-xs text-muted-foreground leading-relaxed">
          <div className="flex items-center gap-1.5">
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
            <span>No active concerns.</span>
          </div>
          <p className="mt-1">
            The guardian generates these from risk notes on indexed
            files. If guardian indexing hasn&apos;t run yet, concerns
            will appear once it completes.
          </p>
        </div>
      )}

      {findings.length > 0 && (
        <div className="space-y-1.5 max-h-[40vh] overflow-y-auto">
          {findings.map((finding) => (
            <FindingRow
              key={finding.digest}
              finding={finding}
              onDismiss={() => handleDismiss(finding.digest)}
            />
          ))}
        </div>
      )}
    </div>
  );
}


function FindingRow({
  finding, onDismiss,
}: {
  finding: AmbientFinding;
  onDismiss: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const [confirmDismiss, setConfirmDismiss] = useState(false);

  const sevClasses = severityClasses(finding.severity);

  return (
    <div className={cn("rounded border", sevClasses.border, sevClasses.bg)}>
      <div className="flex items-stretch">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="flex flex-1 items-start gap-2 px-2.5 py-2 text-left min-w-0"
        >
          {expanded ? (
            <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
          ) : (
            <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
          )}
          <SeverityIcon severity={finding.severity} />
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span
                className={cn(
                  "text-[9px] px-1 rounded uppercase font-semibold tracking-wide",
                  sevClasses.badge,
                )}
              >
                {finding.severity}
              </span>
              <span className={cn("text-xs font-medium break-words", sevClasses.title)}>
                {finding.title}
              </span>
            </div>
            <div className="mt-0.5 text-[11px] text-muted-foreground truncate">
              {finding.file_paths.slice(0, 3).join(", ")}
              {finding.file_paths.length > 3 && (
                <> · +{finding.file_paths.length - 3} more</>
              )}
            </div>
          </div>
        </button>
        {!confirmDismiss && (
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setConfirmDismiss(true);
            }}
            title="Dismiss this concern"
            className="flex items-center justify-center px-2 border-l border-border/60 text-muted-foreground hover:text-foreground hover:bg-background/40"
          >
            <X className="h-3 w-3" />
          </button>
        )}
        {confirmDismiss && (
          <div className="flex items-center gap-1 px-2 border-l border-border/60">
            <span className="text-[10px] text-muted-foreground">Dismiss?</span>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                onDismiss();
              }}
              className="rounded px-1.5 py-0.5 text-[10px] bg-red-950/40 border border-red-700/40 text-red-200 hover:bg-red-900/40"
            >
              Yes
            </button>
            <button
              type="button"
              onClick={(e) => {
                e.stopPropagation();
                setConfirmDismiss(false);
              }}
              className="rounded px-1.5 py-0.5 text-[10px] bg-background/60 border border-border hover:bg-background/80"
            >
              No
            </button>
          </div>
        )}
      </div>

      {expanded && (
        <div className="border-t border-border/60 px-2.5 py-2 text-xs space-y-2">
          <div className="leading-relaxed text-muted-foreground">
            {finding.description}
          </div>
          {finding.evidence && finding.evidence.length > 0 && (
            <div className="space-y-1">
              <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
                Evidence
              </div>
              {finding.evidence.map((ev, i) => (
                <div
                  key={i}
                  className="rounded bg-background/60 border border-border p-2 space-y-0.5"
                >
                  <code className="text-[10px] font-medium break-all">
                    {ev.path}
                  </code>
                  <div className="text-[11px] leading-relaxed">
                    {ev.note}
                  </div>
                </div>
              ))}
            </div>
          )}
          <div className="text-[9px] text-muted-foreground/60 tabular-nums">
            score: {finding.score}
          </div>
        </div>
      )}
    </div>
  );
}


function SeverityIcon({ severity }: { severity: string }) {
  if (severity === "critical") {
    return <AlertOctagon className="mt-0.5 h-3 w-3 shrink-0 text-red-400" />;
  }
  if (severity === "high") {
    return <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-orange-400" />;
  }
  if (severity === "medium") {
    return <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-amber-400" />;
  }
  return <Info className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />;
}


function severityClasses(severity: string) {
  switch (severity) {
    case "critical":
      return {
        border: "border-red-900/50",
        bg: "bg-red-950/20",
        badge: "bg-red-950/40 border border-red-700/40 text-red-200",
        title: "text-red-100",
      };
    case "high":
      return {
        border: "border-orange-900/50",
        bg: "bg-orange-950/20",
        badge: "bg-orange-950/40 border border-orange-700/40 text-orange-200",
        title: "text-orange-100",
      };
    case "medium":
      return {
        border: "border-amber-900/50",
        bg: "bg-amber-950/20",
        badge: "bg-amber-950/40 border border-amber-700/40 text-amber-200",
        title: "text-amber-100",
      };
    default:
      return {
        border: "border-border",
        bg: "bg-background/30",
        badge: "bg-background/60 border border-border text-muted-foreground",
        title: "text-foreground",
      };
  }
}
