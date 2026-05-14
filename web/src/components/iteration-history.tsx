"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Clock, Loader2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import {
  InlineRiskPanel,
  RiskPauseBanner,
  RiskCancellationBanner,
} from "@/components/inline-risk-panel";
import { cancelIteration, proceedIteration } from "@/lib/api";
import { cn } from "@/lib/utils";
import type { Iteration, IterationRisks } from "@/lib/types";

interface IterationHistoryProps {
  iterations: Iteration[];
  /**
   * Iteration seqs currently in flight (queued or running). The history
   * shows them as "running" with a pulsing indicator even before the
   * outcome record lands.
   */
  inFlightSeqs: Set<number>;
  /**
   * Risk records keyed by iteration seq. Each entry may have any
   * combination of pre_risk, post_risk, and cancelled. Absent entries
   * mean no risk records exist yet (either pre-Turn-D.1 iterations or
   * still in flight).
   */
  risksBySeq: Record<number, IterationRisks>;
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
 * Compact sidebar listing past iterations. Each row collapses to a
 * one-liner; clicking expands to show changed files, prompt, AND
 * the guardian's pre/post risk assessments inline (when available).
 *
 * Designed to take ~280px on the left edge. If there are no iterations
 * (fresh build, never iterated), we render nothing — no point in
 * occupying space for an empty list.
 *
 * Risk integration (Turn D.2)
 * ---------------------------
 * For each iteration we receive a possibly-empty IterationRisks bag.
 * When the iteration is paused at pre-flight (pre_risk present but
 * neither post_risk nor cancelled), the card shows Proceed / Cancel
 * buttons. Otherwise the panels render in chronological order:
 *   1. Pre-flight panel (if present)
 *   2. Pause banner (if pre-flight was critical and no decision yet)
 *   3. Cancellation banner (if cancelled)
 *   4. Post-iteration panel (if iteration completed)
 */
export function IterationHistory({
  iterations,
  inFlightSeqs,
  risksBySeq,
  projectId,
  onDecisionMade,
}: IterationHistoryProps) {
  if (iterations.length === 0 && inFlightSeqs.size === 0) {
    return null;
  }

  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Iterations
      </div>
      <div className="space-y-1.5">
        {iterations.map((it) => (
          <IterationRow
            key={it.seq}
            iteration={it}
            running={inFlightSeqs.has(it.seq)}
            risks={risksBySeq[it.seq]}
            projectId={projectId}
            onDecisionMade={onDecisionMade}
          />
        ))}
      </div>
    </div>
  );
}

function IterationRow({
  iteration,
  running,
  risks,
  projectId,
  onDecisionMade,
}: {
  iteration: Iteration;
  running: boolean;
  risks?: IterationRisks;
  projectId: string;
  onDecisionMade?: () => void;
}) {
  // Auto-expand if there's a paused pre-flight awaiting decision —
  // the user needs to see and act on it immediately.
  const isPausedAtPreflight =
    risks?.pre_risk?.severity === "critical" &&
    !risks?.post_risk &&
    !risks?.cancelled &&
    iteration.status === "running";

  const [expanded, setExpanded] = useState(isPausedAtPreflight);
  const [decisionSubmitting, setDecisionSubmitting] = useState(false);
  const [decisionError, setDecisionError] = useState<string | null>(null);

  const isRunning = running || iteration.status === "running";
  const isAutopatch = iteration.autopatch_attempt != null;
  const isCancelled = !!risks?.cancelled;

  const changeCount =
    iteration.changes_applied.length +
    iteration.new_files_created.length +
    iteration.files_deleted.length;

  async function handleProceed() {
    setDecisionSubmitting(true);
    setDecisionError(null);
    try {
      await proceedIteration(projectId, iteration.seq);
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
      await cancelIteration(projectId, iteration.seq);
      if (onDecisionMade) onDecisionMade();
    } catch (err) {
      setDecisionError(
        err instanceof Error ? err.message : "Cancel call failed",
      );
    } finally {
      setDecisionSubmitting(false);
    }
  }

  // Border color: paused-critical gets red; cancelled gets zinc;
  // otherwise unchanged.
  const borderClass = isPausedAtPreflight
    ? "border-red-700/50"
    : isCancelled
      ? "border-zinc-700/40"
      : isRunning
        ? "border-blue-700/50"
        : iteration.failed.length > 0
          ? "border-amber-700/40"
          : isAutopatch
            ? "border-violet-700/40"
            : "border-border";

  return (
    <div
      className={cn(
        "rounded-md border bg-card/60 transition-colors",
        borderClass,
      )}
    >
      <button
        type="button"
        onClick={() => setExpanded((e) => !e)}
        className="flex w-full items-start gap-2 px-3 py-2 text-left"
      >
        {expanded ? (
          <ChevronDown className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="mt-0.5 h-3 w-3 shrink-0 text-muted-foreground" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium tabular-nums">
              #{iteration.seq}
            </span>
            {isAutopatch && (
              <span
                className="rounded border border-violet-700/50 bg-violet-950/40 px-1 text-[9px] uppercase tracking-wide text-violet-300"
                title={`Auto-patch attempt ${iteration.autopatch_attempt}`}
              >
                auto-patch
              </span>
            )}
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
            {!isRunning && iteration.failed.length > 0 && (
              <Badge variant="warning" className="h-4 px-1 text-[9px]">
                {iteration.failed.length} failed
              </Badge>
            )}
            {!isRunning && iteration.failed.length === 0 && changeCount > 0 && (
              <span className="text-[10px] text-muted-foreground">
                {changeCount} file{changeCount === 1 ? "" : "s"}
              </span>
            )}
            {/* Small risk severity dots — visible at-a-glance even
                when the card is collapsed. */}
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
                title={`Post-iteration risk: ${risks.post_risk.severity}`}
                className={cn(
                  "h-1.5 w-1.5 rounded-full ring-1 ring-border",
                  dotForSeverity(risks.post_risk.severity),
                )}
              />
            )}
          </div>
          <div className="mt-0.5 truncate text-xs text-muted-foreground">
            {iteration.rationale || iteration.prompt}
          </div>
        </div>
      </button>

      {expanded && (
        <div className="border-t border-border px-3 py-2 text-xs space-y-2">
          {iteration.prompt && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                Prompt
              </div>
              <div className="mt-0.5 break-words">{iteration.prompt}</div>
            </div>
          )}

          {/* Pre-flight risk panel — shown if available. */}
          {risks?.pre_risk && (
            <InlineRiskPanel
              assessment={risks.pre_risk}
              label="Pre-flight risk"
            />
          )}

          {/* Pause banner — only when pre-flight was critical and
              no decision has been recorded yet. */}
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

          {/* Cancellation banner — when the iteration was aborted. */}
          {isCancelled && risks?.cancelled && (
            <RiskCancellationBanner
              reason={risks.cancelled.reason}
              preRiskSeverity={risks.cancelled.pre_risk_severity}
            />
          )}

          {/* "Generating…" indicator: pre-flight done (non-critical),
              regen in progress, no post-risk yet. Helps the user
              understand which stage they're at. */}
          {isRunning &&
            !isPausedAtPreflight &&
            risks?.pre_risk &&
            !risks?.post_risk && (
              <div className="flex items-center gap-1.5 text-[11px] text-blue-300">
                <Loader2 className="h-3 w-3 animate-spin" />
                Generating files…
              </div>
            )}

          {iteration.changes_applied.length > 0 && (
            <ChangeList
              label="Changed"
              paths={iteration.changes_applied}
              colorClass="text-emerald-400"
            />
          )}
          {iteration.new_files_created.length > 0 && (
            <ChangeList
              label="Created"
              paths={iteration.new_files_created}
              colorClass="text-sky-400"
            />
          )}
          {iteration.files_deleted.length > 0 && (
            <ChangeList
              label="Deleted"
              paths={iteration.files_deleted}
              colorClass="text-zinc-500"
            />
          )}
          {iteration.failed.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-wide text-amber-400">
                Failed
              </div>
              {iteration.failed.map((f, i) => (
                <div key={i} className="mt-0.5 text-xs">
                  <code className="text-amber-300">{f.path}</code>
                  <div className="text-[10px] text-muted-foreground">
                    {f.reason}
                  </div>
                </div>
              ))}
            </div>
          )}

          {/* Post-iteration risk panel — last, after the user sees what
              changed. */}
          {risks?.post_risk && (
            <InlineRiskPanel
              assessment={risks.post_risk}
              label="Post-iteration risk"
            />
          )}

          {(iteration.input_tokens > 0 || iteration.output_tokens > 0) && (
            <div className="flex items-center gap-1 text-[10px] text-muted-foreground">
              <Clock className="h-3 w-3" />
              {iteration.input_tokens.toLocaleString()} in /{" "}
              {iteration.output_tokens.toLocaleString()} out
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ChangeList({
  label,
  paths,
  colorClass,
}: {
  label: string;
  paths: string[];
  colorClass: string;
}) {
  return (
    <div>
      <div className={cn("text-[10px] uppercase tracking-wide", colorClass)}>
        {label}
      </div>
      <ul className="mt-0.5 space-y-0.5">
        {paths.map((p) => (
          <li key={p}>
            <code className="text-xs">{p}</code>
          </li>
        ))}
      </ul>
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
