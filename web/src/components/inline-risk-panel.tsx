"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Loader2, ShieldAlert, ShieldCheck } from "lucide-react";

import { cn } from "@/lib/utils";
import type {
  AttachedRiskAssessment,
  RiskSeverity,
} from "@/lib/types";

/**
 * Compact, inline risk panel rendered inside iteration and fix-all
 * history cards. Smaller and more visually subordinate than the
 * standalone RiskResult panel — it lives inside another card and
 * shouldn't compete with the iteration's own content.
 *
 * Renders:
 *   - Severity badge + confidence indicator (always)
 *   - Plain narrative (always visible when present)
 *   - Concerns chips (collapsed by default, click to expand)
 *   - Technical narrative (separate toggle)
 *   - Affected paths (separate toggle)
 *
 * The label prop distinguishes "Pre-flight" vs "Post-iteration" risk
 * since they have the same visual treatment but different semantic
 * meanings.
 */
interface InlineRiskPanelProps {
  assessment: AttachedRiskAssessment;
  label: string; // "Pre-flight risk" | "Post-iteration risk" | "Pre-flight" | "Post"
  variant?: "compact" | "normal";
}

export function InlineRiskPanel({
  assessment, label, variant = "normal",
}: InlineRiskPanelProps) {
  const [concernsExpanded, setConcernsExpanded] = useState(false);
  const [techExpanded, setTechExpanded] = useState(false);
  const [pathsExpanded, setPathsExpanded] = useState(false);

  // Skip rendering when the analysis was a no-op (empty plan or
  // empty outcome) — the panel adds visual noise without info.
  const isSkipped = assessment.analyzer_model === "(skipped)";
  if (isSkipped) return null;

  return (
    <div
      className={cn(
        "rounded border bg-background/40 p-2 space-y-1.5",
        severityBorderClass(assessment.severity),
      )}
    >
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className={cn(
            "text-[10px] font-semibold uppercase tracking-wide",
            severityTextClass(assessment.severity),
          )}
        >
          {label}
        </span>
        <SeverityBadge severity={assessment.severity} />
        <ConfidenceBar confidence={assessment.confidence} />
        <span className="ml-auto text-[10px] text-muted-foreground tabular-nums">
          {assessment.indexed_summary_count} ctx
        </span>
      </div>

      {/* Plain narrative — always shown when present, that's the
          point of the panel. */}
      {assessment.plain_narrative && (
        <div className="text-xs leading-relaxed">
          {assessment.plain_narrative}
        </div>
      )}

      {/* Concerns toggle — list of per-file flags. */}
      {assessment.concerns.length > 0 && (
        <div>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setConcernsExpanded((v) => !v);
            }}
            className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
          >
            {concernsExpanded ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            {assessment.concerns.length} concern
            {assessment.concerns.length === 1 ? "" : "s"}
          </button>
          {concernsExpanded && (
            <div className="mt-1 space-y-1">
              {assessment.concerns.map((c, i) => (
                <div
                  key={`${c.path}-${i}`}
                  className="flex items-start gap-1.5 rounded border border-border bg-background/60 p-1.5"
                >
                  <SeverityBadge severity={c.severity} small />
                  <div className="min-w-0 flex-1">
                    <code className="text-[10px] text-muted-foreground break-all">
                      {c.path}
                    </code>
                    <div className="text-xs mt-0.5">{c.reason}</div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Suggested sequencing — uncommon but valuable when present. */}
      {assessment.suggested_sequencing.length > 0 && (
        <div className="text-[10px]">
          <span className="font-semibold uppercase tracking-wide text-muted-foreground">
            Sequence:{" "}
          </span>
          <span className="text-foreground">
            {assessment.suggested_sequencing.join(" → ")}
          </span>
        </div>
      )}

      {/* Technical narrative toggle. */}
      {assessment.technical_narrative && (
        <div>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setTechExpanded((v) => !v);
            }}
            className="flex items-center gap-1 text-[10px] text-violet-400 hover:text-violet-300"
          >
            {techExpanded ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            {techExpanded ? "Hide" : "Show"} technical detail
          </button>
          {techExpanded && (
            <div className="mt-1 text-xs leading-relaxed border-l-2 border-violet-900/40 pl-2 text-muted-foreground">
              {assessment.technical_narrative}
            </div>
          )}
        </div>
      )}

      {/* Affected paths toggle — usually redundant with concerns list
          but helpful when the model didn't enumerate concerns for
          every affected file. */}
      {assessment.affected_paths.length > 0 && (
        <div>
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              setPathsExpanded((v) => !v);
            }}
            className="flex items-center gap-1 text-[10px] text-muted-foreground hover:text-foreground"
          >
            {pathsExpanded ? (
              <ChevronDown className="h-3 w-3" />
            ) : (
              <ChevronRight className="h-3 w-3" />
            )}
            {assessment.affected_paths.length} affected file
            {assessment.affected_paths.length === 1 ? "" : "s"}
          </button>
          {pathsExpanded && (
            <div className="mt-1 flex flex-wrap gap-1">
              {assessment.affected_paths.slice(0, 30).map((p) => (
                <code
                  key={p}
                  className="text-[10px] px-1.5 py-0.5 rounded bg-background/60 border border-border break-all"
                >
                  {p}
                </code>
              ))}
              {assessment.affected_paths.length > 30 && (
                <span className="text-[10px] text-muted-foreground">
                  +{assessment.affected_paths.length - 30} more
                </span>
              )}
            </div>
          )}
        </div>
      )}

      <div className="text-[9px] text-muted-foreground/60">
        {assessment.analyzer_model}
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Visual helpers — small and reused.
// ---------------------------------------------------------------------------

function SeverityBadge({
  severity, small = false,
}: { severity: RiskSeverity; small?: boolean }) {
  const config: Record<RiskSeverity, { label: string; cls: string }> = {
    low: {
      label: "Low",
      cls: "bg-emerald-950/40 border-emerald-700/40 text-emerald-300",
    },
    medium: {
      label: "Medium",
      cls: "bg-amber-950/40 border-amber-700/40 text-amber-300",
    },
    high: {
      label: "High",
      cls: "bg-orange-950/40 border-orange-700/40 text-orange-300",
    },
    critical: {
      label: "Critical",
      cls: "bg-red-950/40 border-red-700/40 text-red-300",
    },
  };
  const c = config[severity] ?? config.low;
  const Icon = severity === "low" ? ShieldCheck : ShieldAlert;
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded border px-1.5 font-medium",
        small ? "py-0 text-[9px]" : "py-0.5 text-[10px]",
        c.cls,
      )}
    >
      <Icon className={small ? "h-2.5 w-2.5" : "h-3 w-3"} />
      {c.label}
    </span>
  );
}

function ConfidenceBar({ confidence }: { confidence: number }) {
  const pct = Math.round(confidence * 100);
  const colorClass =
    confidence >= 0.75
      ? "bg-emerald-500"
      : confidence >= 0.5
        ? "bg-amber-500"
        : "bg-red-500";
  return (
    <span className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
      <span className="relative w-8 h-1 rounded-full bg-border overflow-hidden">
        <span
          className={cn("absolute inset-y-0 left-0", colorClass)}
          style={{ width: `${pct}%` }}
        />
      </span>
      <span className="tabular-nums">{pct}%</span>
    </span>
  );
}


function severityBorderClass(severity: RiskSeverity): string {
  switch (severity) {
    case "critical":
      return "border-red-700/40";
    case "high":
      return "border-orange-700/40";
    case "medium":
      return "border-amber-700/40";
    default:
      return "border-emerald-900/40";
  }
}

function severityTextClass(severity: RiskSeverity): string {
  switch (severity) {
    case "critical":
      return "text-red-300";
    case "high":
      return "text-orange-300";
    case "medium":
      return "text-amber-300";
    default:
      return "text-emerald-300";
  }
}


// ---------------------------------------------------------------------------
// Pre-flight pause state — used by both iteration and fix-all when the
// pre-flight assessment is critical and the user must decide.
// ---------------------------------------------------------------------------

interface RiskPauseBannerProps {
  severity: RiskSeverity;
  onProceed: () => void;
  onCancel: () => void;
  submitting: boolean;
}

export function RiskPauseBanner({
  severity, onProceed, onCancel, submitting,
}: RiskPauseBannerProps) {
  return (
    <div className="rounded border border-red-700/50 bg-red-950/30 p-2 space-y-1.5">
      <div className="flex items-center gap-2">
        <ShieldAlert className="h-4 w-4 text-red-400 flex-shrink-0" />
        <div className="text-xs font-semibold text-red-300">
          Paused: {severity} risk detected
        </div>
      </div>
      <div className="text-[11px] text-muted-foreground">
        The guardian flagged this change as risky. Review the assessment
        above and confirm to continue, or cancel to abandon.
      </div>
      <div className="flex gap-2 pt-0.5">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onProceed();
          }}
          disabled={submitting}
          className="flex items-center gap-1 rounded border border-amber-700/50 bg-amber-950/40 px-2 py-1 text-[11px] font-medium text-amber-200 hover:bg-amber-900/40 disabled:opacity-50"
        >
          {submitting && <Loader2 className="h-3 w-3 animate-spin" />}
          Proceed anyway
        </button>
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            onCancel();
          }}
          disabled={submitting}
          className="rounded border border-border bg-background/60 px-2 py-1 text-[11px] hover:bg-background/80 disabled:opacity-50"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}


// ---------------------------------------------------------------------------
// Cancellation summary — shown when an iteration/fix-all was aborted
// at the pre-flight stage (either by user cancel or timeout).
// ---------------------------------------------------------------------------

interface RiskCancellationBannerProps {
  reason: string;       // "cancel" | "timeout"
  preRiskSeverity: string;
}

export function RiskCancellationBanner({
  reason, preRiskSeverity,
}: RiskCancellationBannerProps) {
  const label =
    reason === "timeout"
      ? "Auto-cancelled after timeout"
      : "Cancelled by user";
  return (
    <div className="rounded border border-zinc-700/40 bg-zinc-900/40 p-2">
      <div className="flex items-center gap-1.5 text-[11px] text-zinc-400">
        <ShieldCheck className="h-3 w-3" />
        <span>
          {label} at pre-flight ({preRiskSeverity} risk).
        </span>
      </div>
    </div>
  );
}
