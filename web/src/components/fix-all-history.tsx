"use client";

import { useState } from "react";
import {
  ChevronDown, ChevronRight, Loader2, Square, Wand2,
} from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  InlineRiskPanel,
  RiskPauseBanner,
  RiskCancellationBanner,
} from "@/components/inline-risk-panel";
import { cancelFixAll, proceedFixAll, stopFixAll } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { FixAllPass, FixAllRisks } from "@/lib/types";

interface FixAllHistoryProps {
  passes: FixAllPass[];
  /**
   * Risk records keyed by fix-all seq. Same shape as iteration risks.
   */
  risksBySeq: Record<number, FixAllRisks>;
  /**
   * Project ID — needed to wire proceed/cancel buttons.
   */
  projectId: string;
  /**
   * Bumped by the parent after a successful proceed/cancel so the
   * card-level state can refresh.
   */
  onDecisionMade?: () => void;
}

/**
 * Compact list of past fix-all passes, with the report expanded
 * inline. Rendered above (or next to) the iteration history so the
 * user can see what each manual fix-all attempt accomplished.
 *
 * Risk integration (Turn D.2) is identical in shape to iteration
 * history: pre/post panels render inline; critical pre-flight pauses
 * the pass with Proceed/Cancel buttons.
 */
export function FixAllHistory({
  passes, risksBySeq, projectId, onDecisionMade,
}: FixAllHistoryProps) {
  if (passes.length === 0) return null;
  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Fix-all passes
      </div>
      <div className="space-y-1.5">
        {passes.map((p) => (
          <FixAllRow
            key={p.seq}
            pass={p}
            risks={risksBySeq[p.seq]}
            projectId={projectId}
            onDecisionMade={onDecisionMade}
          />
        ))}
      </div>
    </div>
  );
}

function FixAllRow({
  pass, risks, projectId, onDecisionMade,
}: {
  pass: FixAllPass;
  risks?: FixAllRisks;
  projectId: string;
  onDecisionMade?: () => void;
}) {
  const isPausedAtPreflight =
    risks?.pre_risk?.severity === "critical" &&
    !risks?.post_risk &&
    !risks?.cancelled &&
    pass.status === "running";

  const [expanded, setExpanded] = useState(
    // Auto-expand the most recent complete pass OR a paused critical.
    isPausedAtPreflight || pass.status === "complete",
  );
  const [decisionSubmitting, setDecisionSubmitting] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);
  const [stopRequested, setStopRequested] = useState(false);
  const [stopError, setStopError] = useState<string | null>(null);

  const isRunning = pass.status === "running";
  const isClean = !isRunning && pass.post_count === 0;
  const isCancelled = !!risks?.cancelled;

  async function handleProceed() {
    setDecisionSubmitting(true);
    setDecisionError(null);
    try {
      await proceedFixAll(projectId, pass.seq);
      if (onDecisionMade) onDecisionMade();
    } catch (err) {
      setDecisionError(
        err instanceof Error ? err.message : "Proceed call failed",
      );
    } finally {
      setDecisionSubmitting(false);
    }
  }

  async function handleCancel() {
    setDecisionSubmitting(true);
    setDecisionError(null);
    try {
      await cancelFixAll(projectId, pass.seq);
      if (onDecisionMade) onDecisionMade();
    } catch (err) {
      setDecisionError(
        err instanceof Error ? err.message : "Cancel call failed",
      );
    } finally {
      setDecisionSubmitting(false);
    }
  }

  async function handleStop(e: React.MouseEvent) {
    // Stop button lives inside the row's expand button. Don't toggle
    // expand when the user clicks stop.
    e.stopPropagation();
    setStopRequested(true);
    setStopError(null);
    try {
      await stopFixAll(projectId, pass.seq);
      if (onDecisionMade) onDecisionMade();
    } catch (err) {
      setStopError(
        err instanceof Error ? err.message : "Stop request failed",
      );
      setStopRequested(false);
    }
  }

  const borderClass = isPausedAtPreflight
    ? "border-red-700/50"
    : isCancelled
      ? "border-zinc-700/40"
      : isRunning
        ? "border-violet-700/50"
        : isClean
          ? "border-emerald-700/40"
          : "border-border";

  return (
    <div
      className={cn(
        "rounded-md border bg-card/60 transition-colors",
        borderClass,
      )}
    >
      <div className="flex items-stretch">
        <button
          type="button"
          onClick={() => setExpanded((e) => !e)}
          className="flex flex-1 items-start gap-2 px-3 py-2 text-left min-w-0"
        >
        {expanded ? (
          <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
        )}
        <Wand2 className="mt-0.5 h-3 w-3 shrink-0 text-violet-400" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium tabular-nums">
              Pass #{pass.seq}
            </span>
            {isPausedAtPreflight && (
              <Badge
                variant="warning"
                className="h-4 border-red-700/50 bg-red-950/40 px-1 text-[9px] text-red-300"
              >
                paused
              </Badge>
            )}
            {!isPausedAtPreflight && isCancelled && (
              <Badge
                variant="provider"
                className="h-4 border-zinc-700/50 bg-zinc-900/40 px-1 text-[9px] text-zinc-400"
              >
                cancelled
              </Badge>
            )}
            {!isPausedAtPreflight && !isCancelled && isRunning && (
              <Badge variant="warning" className="h-4 px-1 text-[9px]">
                running
              </Badge>
            )}
            {!isRunning && isClean && (
              <Badge
                variant="provider"
                className="h-4 border-emerald-700/50 bg-emerald-950/40 px-1 text-[9px] text-emerald-300"
              >
                clean
              </Badge>
            )}
            {!isRunning && !isClean && pass.fixed != null && (
              <span className="text-[10px] text-muted-foreground tabular-nums">
                fixed {pass.fixed}, {pass.post_count} left
              </span>
            )}
            {/* Severity dots, same as iteration history. */}
            {risks?.pre_risk && (
              <span
                title={`Pre-flight risk: ${risks.pre_risk.severity}`}
                className={cn(
                  "h-1.5 w-1.5 rounded-full",
                  dotForSeverity(risks.pre_risk.severity),
                )}
              />
            )}
            {risks?.post_risk && (
              <span
                title={`Post-pass risk: ${risks.post_risk.severity}`}
                className={cn(
                  "h-1.5 w-1.5 rounded-full ring-1 ring-border",
                  dotForSeverity(risks.post_risk.severity),
                )}
              />
            )}
          </div>
          {!expanded && (
            <div className="mt-0.5 truncate text-xs text-muted-foreground">
              {isRunning
                ? `Fixing ${pass.issue_count} issue(s) across ${pass.files_affected} file(s)…`
                : pass.report
                  ? pass.report.split("\n")[0]
                  : "—"}
            </div>
          )}
        </div>
        </button>
        {/* Stop button — only shown while the pass is actively running
            AND not paused at pre-flight (paused uses the proceed/cancel
            buttons inside the card body instead). Sibling of the expand
            button so we don't nest buttons. */}
        {isRunning && !isPausedAtPreflight && !isCancelled && (
          <button
            type="button"
            onClick={handleStop}
            disabled={stopRequested}
            title={stopRequested ? "Stop requested" : "Stop this fix-all pass"}
            className={cn(
              "flex items-center justify-center px-2 border-l border-border",
              "text-muted-foreground hover:text-red-300 hover:bg-red-950/30",
              "disabled:opacity-50 disabled:cursor-not-allowed",
            )}
          >
            {stopRequested ? (
              <Loader2 className="h-3.5 w-3.5 animate-spin" />
            ) : (
              <Square className="h-3.5 w-3.5" fill="currentColor" />
            )}
          </button>
        )}
      </div>
      {stopError && (
        <div className="border-t border-border px-3 py-1 text-[11px] text-red-300">
          {stopError}
        </div>
      )}
      {expanded && (
        <div className="border-t border-border px-3 py-2 text-xs space-y-2">
          {risks?.pre_risk && (
            <InlineRiskPanel
              assessment={risks.pre_risk}
              label="Pre-flight risk"
            />
          )}

          {isPausedAtPreflight && (
            <>
              <RiskPauseBanner
                severity="critical"
                onProceed={handleProceed}
                onCancel={handleCancel}
                submitting={decisionSubmitting}
              />
              {decisionError && (
                <div className="text-[11px] text-red-300">{decisionError}</div>
              )}
            </>
          )}

          {isCancelled && risks?.cancelled && (
            <RiskCancellationBanner
              reason={risks.cancelled.reason}
              preRiskSeverity={risks.cancelled.pre_risk_severity}
            />
          )}

          {isRunning &&
            !isPausedAtPreflight &&
            risks?.pre_risk &&
            !risks?.post_risk && (
              <div className="flex items-center gap-1.5 text-[11px] text-blue-300">
                <Loader2 className="h-3 w-3 animate-spin" />
                Fixing files…
              </div>
            )}

          {pass.report && (
            <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-muted-foreground">
              {pass.report}
            </pre>
          )}

          {risks?.post_risk && (
            <InlineRiskPanel
              assessment={risks.post_risk}
              label="Post-pass risk"
            />
          )}
        </div>
      )}
    </div>
  );
}

function dotForSeverity(severity: string): string {
  switch (severity) {
    case "critical":
      return "bg-red-500";
    case "high":
      return "bg-orange-500";
    case "medium":
      return "bg-amber-500";
    case "low":
      return "bg-emerald-500";
    default:
      return "bg-muted-foreground";
  }
}
