"use client";

import { useCallback, useEffect, useState } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  HelpCircle,
  Loader2,
  RefreshCw,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getDecisions, resolveDecision } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { DecisionNeeded } from "@/lib/types";

interface DecisionsNeededPanelProps {
  projectId: string;
  /**
   * Bumped by the parent when a decision_needed:* ledger entry
   * arrives via WebSocket. Forces a refetch so users see new
   * decisions stream in as fix-all runs.
   */
  refreshKey?: number;
}

/**
 * Decisions Needed panel — surfaces findings the Builder declined to
 * fix because they require human input. Each row asks a specific
 * question; the user answers with a value, or dismisses if the
 * placeholder is intentional.
 *
 * When a user provides a value, the next fix-all pass synthesizes a
 * Finding from the answer and queues it through the normal flow.
 * The Builder no longer has to invent fake URLs or guess architectural
 * intent.
 *
 * Empty state is good — it means the Builder didn't refuse anything
 * because nothing in the project requires opinion-shaped fixes.
 */
export function DecisionsNeededPanel({
  projectId, refreshKey = 0,
}: DecisionsNeededPanelProps) {
  const [decisions, setDecisions] = useState<DecisionNeeded[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState<Set<string>>(new Set());

  const refetch = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await getDecisions(projectId);
      setDecisions(resp.decisions);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Failed to load decisions",
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
    action: "provide_value" | "dismiss",
    value?: string,
  ) {
    setPending((p) => new Set(p).add(digest));
    const snapshot = decisions;
    setDecisions((prev) => prev.filter((d) => d.digest !== digest));
    try {
      await resolveDecision(projectId, digest, action, value);
    } catch (err) {
      console.error("[decision] resolve failed:", err);
      setDecisions(snapshot);
    } finally {
      setPending((p) => {
        const next = new Set(p);
        next.delete(digest);
        return next;
      });
    }
  }

  const architecturalCount = decisions.filter(
    (d) => d.decision_type === "architectural",
  ).length;

  return (
    <div className="rounded-md border border-border bg-card/50 p-4 space-y-3">
      <div className="flex items-center gap-2">
        <HelpCircle className="h-4 w-4 text-indigo-300" />
        <h3 className="text-sm font-semibold tracking-tight">
          Decisions Needed
        </h3>
        <span className="text-xs text-muted-foreground tabular-nums">
          {decisions.length}
          {architecturalCount > 0 && (
            <span className="ml-2 text-indigo-200">
              · {architecturalCount} architectural
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

      {loading && decisions.length === 0 && (
        <div className="text-xs text-muted-foreground">Loading…</div>
      )}

      {!loading && decisions.length === 0 && !error && (
        <div className="text-xs text-muted-foreground leading-relaxed">
          <div className="flex items-center gap-1.5">
            <CheckCircle2 className="h-3.5 w-3.5 text-emerald-400" />
            <span>No pending decisions.</span>
          </div>
          <p className="mt-1">
            When the Builder declines to fix a finding because it
            needs your input — a real URL, an architectural choice,
            an error contract — it appears here for you to resolve.
          </p>
        </div>
      )}

      {decisions.length > 0 && (
        <div className="space-y-2 max-h-[50vh] overflow-y-auto">
          {decisions.map((d) => (
            <DecisionRow
              key={d.digest}
              decision={d}
              isPending={pending.has(d.digest)}
              onResolve={handleResolve}
            />
          ))}
        </div>
      )}
    </div>
  );
}


function DecisionRow({
  decision, isPending, onResolve,
}: {
  decision: DecisionNeeded;
  isPending: boolean;
  onResolve: (
    digest: string,
    action: "provide_value" | "dismiss",
    value?: string,
  ) => void;
}) {
  const [expanded, setExpanded] = useState(true);
  const [inputValue, setInputValue] = useState("");

  const typeClass = typeClassFor(decision.decision_type);

  return (
    <div className={cn("rounded border", typeClass.border, typeClass.bg)}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="w-full px-2.5 py-2 flex items-start gap-2 text-left"
      >
        {expanded ? (
          <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-1.5 flex-wrap">
            <span
              className={cn(
                "text-[9px] px-1 rounded uppercase font-semibold tracking-wide",
                typeClass.badge,
              )}
            >
              {decision.decision_type}
            </span>
            <code className="text-xs font-medium break-all">
              {decision.file_path}
            </code>
            {decision.line ? (
              <span className="text-[10px] text-muted-foreground tabular-nums">
                line {decision.line}
              </span>
            ) : null}
          </div>
          {!expanded && (
            <div className="mt-0.5 truncate text-[11px] text-muted-foreground">
              {decision.decision_needed}
            </div>
          )}
        </div>
      </button>

      {expanded && (
        <div className="border-t border-border/40 px-2.5 py-2 space-y-2">
          <div>
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
              The Builder asks:
            </div>
            <div className="mt-0.5 text-[12px] leading-relaxed font-medium">
              {decision.decision_needed}
            </div>
          </div>

          {decision.blocking_info && (
            <div className="text-[11px] text-muted-foreground leading-relaxed">
              <span className="text-muted-foreground/70">Context: </span>
              {decision.blocking_info}
            </div>
          )}

          <div className="text-[11px] text-muted-foreground leading-relaxed">
            <span className="text-muted-foreground/70">Original finding: </span>
            <span className="italic">{decision.issue}</span>
          </div>

          <div className="space-y-1.5 pt-1">
            <div className="text-[9px] uppercase tracking-wide text-muted-foreground">
              Your answer
            </div>
            <Input
              value={inputValue}
              onChange={(e) => setInputValue(e.target.value)}
              placeholder={placeholderFor(decision.decision_type)}
              disabled={isPending}
              className="text-xs h-7"
              onKeyDown={(e) => {
                if (e.key === "Enter" && inputValue.trim() && !isPending) {
                  onResolve(decision.digest, "provide_value", inputValue.trim());
                }
              }}
            />
            <div className="flex items-center gap-1.5">
              <button
                type="button"
                disabled={isPending || !inputValue.trim()}
                onClick={() =>
                  onResolve(decision.digest, "provide_value", inputValue.trim())
                }
                className={cn(
                  "flex-1 rounded py-1 text-[10px] font-medium border transition-colors disabled:opacity-50",
                  typeClass.button,
                )}
              >
                {isPending ? (
                  <span className="flex items-center justify-center gap-1">
                    <Loader2 className="h-2.5 w-2.5 animate-spin" />
                    Saving…
                  </span>
                ) : (
                  <>Use this value (queues for next fix-all)</>
                )}
              </button>
              <button
                type="button"
                disabled={isPending}
                onClick={() => onResolve(decision.digest, "dismiss")}
                title="The placeholder/missing value is intentional"
                className="rounded px-2 py-1 text-[10px] bg-background/60 border border-border text-muted-foreground hover:bg-background/80 disabled:opacity-50"
              >
                <span className="flex items-center gap-1">
                  <X className="h-2.5 w-2.5" />
                  Leave as-is
                </span>
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


function typeClassFor(t: string) {
  switch (t) {
    case "architectural":
      return {
        border: "border-indigo-900/40",
        bg: "bg-indigo-950/10",
        badge: "bg-indigo-950/40 border border-indigo-700/40 text-indigo-200",
        button:
          "border-indigo-700/50 bg-indigo-950/30 text-indigo-100 hover:bg-indigo-900/40",
      };
    case "contract":
      return {
        border: "border-violet-900/40",
        bg: "bg-violet-950/10",
        badge: "bg-violet-950/40 border border-violet-700/40 text-violet-200",
        button:
          "border-violet-700/50 bg-violet-950/30 text-violet-100 hover:bg-violet-900/40",
      };
    case "value":
      return {
        border: "border-sky-900/40",
        bg: "bg-sky-950/10",
        badge: "bg-sky-950/40 border border-sky-700/40 text-sky-200",
        button:
          "border-sky-700/50 bg-sky-950/30 text-sky-100 hover:bg-sky-900/40",
      };
    case "policy":
    default:
      return {
        border: "border-amber-900/40",
        bg: "bg-amber-950/10",
        badge: "bg-amber-950/40 border border-amber-700/40 text-amber-200",
        button:
          "border-amber-700/50 bg-amber-950/30 text-amber-100 hover:bg-amber-900/40",
      };
  }
}


function placeholderFor(t: string): string {
  switch (t) {
    case "value":
      return "e.g. https://github.com/myorg/myrepo";
    case "architectural":
      return "e.g. 'mandatory dependency' or 'optional, declare in PSData'";
    case "contract":
      return "e.g. 'throw [System.IO.FileNotFoundException]'";
    case "policy":
      return "e.g. 'fall back to in-memory if storage unavailable'";
    default:
      return "Your answer…";
  }
}
