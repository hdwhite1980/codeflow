"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle,
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  Loader2,
  RefreshCw,
  Scale,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { getDisagreements, resolveDisagreement } from "@/lib/api";
import { cn } from "@/lib/utils";
import type {
  AuditorDisagreement,
  DisagreementFinding,
} from "@/lib/types";

interface AuditorDisagreementsPanelProps {
  projectId: string;
  /**
   * Bumped by the parent when an audit_disagreement:* ledger entry
   * arrives via WebSocket. Forces a refetch so users see new
   * disagreements appear without manual reload.
   */
  refreshKey?: number;
}

/**
 * Auditor Disagreements panel — surfaces cases where OpenAI and
 * Gemini contradicted each other on the same finding, and asks the
 * user to pick a winner (or dismiss both).
 *
 * Until the user resolves a disagreement, neither auditor's
 * suggestion gets auto-fixed. Resolution writes a ledger record;
 * the next fix-all pass re-injects the chosen finding through the
 * normal flow.
 *
 * Empty state is friendly — most projects shouldn't have any
 * disagreements. Showing "0 active disagreements" with a green
 * checkmark is the expected baseline.
 */
export function AuditorDisagreementsPanel({
  projectId, refreshKey = 0,
}: AuditorDisagreementsPanelProps) {
  const [disagreements, setDisagreements] = useState<AuditorDisagreement[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Set<string>>(new Set());

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await getDisagreements(projectId);
      setDisagreements(resp.disagreements);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load disagreements",
      );
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    refetch();
  }, [refetch, refreshKey]);

  async function handleResolve(
    digest: string,
    action: "queue_fix" | "dismiss_both",
    chosenAuditor?: string,
  ) {
    setPending((p) => new Set(p).add(digest));
    // Optimistic UI: drop the row immediately.
    const snapshot = disagreements;
    setDisagreements((prev) => prev.filter((d) => d.digest !== digest));
    try {
      await resolveDisagreement(projectId, digest, action, chosenAuditor);
    } catch (err) {
      console.error("[disagreement] resolve failed:", err);
      // Roll back the optimistic update.
      setDisagreements(snapshot);
    } finally {
      setPending((p) => {
        const next = new Set(p);
        next.delete(digest);
        return next;
      });
    }
  }

  return (
    <div className="rounded-md border border-border bg-card/50 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <Scale className="h-4 w-4 text-amber-300" />
        <h3 className="text-sm font-semibold tracking-tight">
          Auditor Disagreements
        </h3>
        <span className="text-xs text-muted-foreground tabular-nums">
          {disagreements.length}
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

      {loading && disagreements.length === 0 && (
        <div className="text-xs text-muted-foreground">Loading…</div>
      )}

      {!loading && disagreements.length === 0 && !error && (
        <div className="text-xs text-muted-foreground leading-relaxed">
          <div className="flex items-center gap-1.5">
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
            <span>No active disagreements.</span>
          </div>
          <p className="mt-1">
            Fix-all surfaces here when auditors give contradictory
            suggestions for the same code. Empty is healthy — it means
            your auditors are agreeing.
          </p>
        </div>
      )}

      {disagreements.length > 0 && (
        <div className="space-y-2 max-h-[40vh] overflow-y-auto">
          {disagreements.map((d) => (
            <DisagreementRow
              key={d.digest}
              disagreement={d}
              isPending={pending.has(d.digest)}
              onResolve={handleResolve}
            />
          ))}
        </div>
      )}
    </div>
  );
}


function DisagreementRow({
  disagreement, isPending, onResolve,
}: {
  disagreement: AuditorDisagreement;
  isPending: boolean;
  onResolve: (
    digest: string,
    action: "queue_fix" | "dismiss_both",
    chosenAuditor?: string,
  ) => void;
}) {
  const [expanded, setExpanded] = useState(true);  // expanded by default
  const sortedFindings = [...disagreement.findings].sort(
    (a, b) => a.auditor.localeCompare(b.auditor),
  );

  return (
    <div className="rounded border border-amber-900/40 bg-amber-950/10">
      <div className="px-2.5 py-2 flex items-start gap-2">
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="mt-0.5"
        >
          {expanded ? (
            <ChevronDown className="h-3 w-3 text-muted-foreground" />
          ) : (
            <ChevronRight className="h-3 w-3 text-muted-foreground" />
          )}
        </button>
        <AlertTriangle className="mt-0.5 h-3 w-3 shrink-0 text-amber-400" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <code className="text-xs font-medium break-all">
              {disagreement.file_path}
            </code>
            {disagreement.line ? (
              <span className="text-[10px] text-muted-foreground tabular-nums">
                line {disagreement.line}
              </span>
            ) : null}
            <span className="text-[9px] text-muted-foreground/70">
              · {sortedFindings.length} auditors disagree
            </span>
          </div>
        </div>
      </div>

      {expanded && (
        <div className="border-t border-amber-900/40 px-2.5 py-2 space-y-2">
          <p className="text-[11px] text-muted-foreground leading-relaxed">
            These auditors flagged the same code with contradictory
            suggestions. Pick which one to act on, or dismiss both.
          </p>
          <div className="space-y-1.5">
            {sortedFindings.map((f) => (
              <AuditorPositionCard
                key={f.auditor}
                finding={f}
                isPending={isPending}
                onChoose={() =>
                  onResolve(disagreement.digest, "queue_fix", f.auditor)
                }
              />
            ))}
          </div>
          <div className="flex items-center gap-1.5 pt-1 border-t border-amber-900/30">
            <span className="text-[10px] text-muted-foreground">
              Or:
            </span>
            <button
              type="button"
              disabled={isPending}
              onClick={() =>
                onResolve(disagreement.digest, "dismiss_both")
              }
              className="rounded px-2 py-0.5 text-[10px] bg-background/60 border border-border text-muted-foreground hover:bg-background/80 disabled:opacity-50"
            >
              {isPending ? (
                <span className="flex items-center gap-1">
                  <Loader2 className="h-2.5 w-2.5 animate-spin" />
                  Resolving…
                </span>
              ) : (
                <span className="flex items-center gap-1">
                  <X className="h-2.5 w-2.5" />
                  Dismiss both (neither is right)
                </span>
              )}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}


function AuditorPositionCard({
  finding, isPending, onChoose,
}: {
  finding: DisagreementFinding;
  isPending: boolean;
  onChoose: () => void;
}) {
  // Per-auditor color so the user can keep them straight at a glance.
  const accentClass = auditorAccentClass(finding.auditor);

  return (
    <div
      className={cn(
        "rounded border p-2 space-y-1.5",
        accentClass.border,
        accentClass.bg,
      )}
    >
      <div className="flex items-center gap-1.5">
        <span
          className={cn(
            "text-[9px] px-1 rounded uppercase font-semibold tracking-wide",
            accentClass.badge,
          )}
        >
          {finding.auditor}
        </span>
        <span className="text-[9px] text-muted-foreground uppercase">
          {finding.severity}
        </span>
      </div>
      <div className="text-[11px] leading-relaxed">{finding.issue}</div>
      {finding.suggestion && (
        <div className="text-[11px] leading-relaxed text-muted-foreground italic">
          Suggested: {finding.suggestion}
        </div>
      )}
      <button
        type="button"
        disabled={isPending}
        onClick={onChoose}
        className={cn(
          "w-full rounded py-1 text-[10px] font-medium border transition-colors disabled:opacity-50",
          accentClass.button,
        )}
      >
        {isPending ? (
          <span className="flex items-center justify-center gap-1">
            <Loader2 className="h-2.5 w-2.5 animate-spin" />
            Choosing…
          </span>
        ) : (
          <>Use this position and queue for fix-all</>
        )}
      </button>
    </div>
  );
}


function auditorAccentClass(auditor: string) {
  const a = auditor.toLowerCase();
  if (a.includes("openai") || a === "gpt") {
    return {
      border: "border-emerald-900/40",
      bg: "bg-emerald-950/10",
      badge: "bg-emerald-950/40 border border-emerald-700/40 text-emerald-200",
      button:
        "border-emerald-700/50 bg-emerald-950/30 text-emerald-100 hover:bg-emerald-900/40",
    };
  }
  if (a.includes("gemini") || a === "google") {
    return {
      border: "border-sky-900/40",
      bg: "bg-sky-950/10",
      badge: "bg-sky-950/40 border border-sky-700/40 text-sky-200",
      button:
        "border-sky-700/50 bg-sky-950/30 text-sky-100 hover:bg-sky-900/40",
    };
  }
  return {
    border: "border-border",
    bg: "bg-background/30",
    badge: "bg-background/60 border border-border text-muted-foreground",
    button:
      "border-border bg-background/60 text-foreground hover:bg-background/80",
  };
}
