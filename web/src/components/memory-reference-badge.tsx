"use client";

import { useState } from "react";
import { Brain, ChevronDown, ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

interface MemoryReferenceBadgeProps {
  /**
   * Number of guardian-indexed files referenced. When 0, the badge
   * still renders but with a muted "no memory yet" state — useful
   * for projects that haven't been indexed.
   */
  count: number;
  /**
   * The actual paths. Shown in a collapsible list under the badge.
   * Can be omitted when only the count is known (e.g. summary-level
   * counts from a risk assessment).
   */
  paths?: string[];
  /**
   * Variant — "inline" is small and lives in cards; "prominent" is
   * the bigger version used in standalone panels.
   */
  variant?: "inline" | "prominent";
  /**
   * Optional label override. Defaults to "Guardian referenced N file(s)".
   */
  label?: string;
}

/**
 * Memory visibility badge — surfaces "the guardian read N file summaries
 * to inform this iteration" so users see the memory layer doing work.
 *
 * Click to expand the file list. The badge is silent when count is 0
 * unless the consumer explicitly renders it (some cards show "Guardian:
 * no memory yet" as a hint to index the project).
 *
 * Design intent: the badge should feel like *bragging* — "we did real
 * work for you" — not like a debug counter. Hence the brain icon and
 * the explicit "Guardian referenced" phrasing rather than a bare number.
 */
export function MemoryReferenceBadge({
  count, paths, variant = "inline", label,
}: MemoryReferenceBadgeProps) {
  const [expanded, setExpanded] = useState(false);

  if (count === 0 && !paths) return null;

  const finalLabel =
    label ?? `Guardian referenced ${count} file${count === 1 ? "" : "s"}`;
  const canExpand = !!paths && paths.length > 0;

  if (variant === "prominent") {
    return (
      <div className="rounded border border-violet-900/40 bg-violet-950/20 p-2 space-y-1.5">
        <button
          type="button"
          onClick={(e) => {
            e.stopPropagation();
            if (canExpand) setExpanded((v) => !v);
          }}
          className={cn(
            "flex items-center gap-2 text-xs font-medium text-violet-300 w-full",
            canExpand && "hover:text-violet-200",
          )}
          disabled={!canExpand}
        >
          <Brain className="h-3.5 w-3.5" />
          <span className="flex-1 text-left">{finalLabel}</span>
          {canExpand && (
            expanded
              ? <ChevronDown className="h-3 w-3" />
              : <ChevronRight className="h-3 w-3" />
          )}
        </button>
        {expanded && paths && (
          <div className="flex flex-wrap gap-1 pl-5">
            {paths.slice(0, 30).map((p) => (
              <code
                key={p}
                className="text-[10px] px-1.5 py-0.5 rounded bg-background/60 border border-border break-all"
              >
                {p}
              </code>
            ))}
            {paths.length > 30 && (
              <span className="text-[10px] text-muted-foreground">
                +{paths.length - 30} more
              </span>
            )}
          </div>
        )}
      </div>
    );
  }

  // Inline variant — smaller, fits inside other cards.
  return (
    <div>
      <button
        type="button"
        onClick={(e) => {
          e.stopPropagation();
          if (canExpand) setExpanded((v) => !v);
        }}
        className={cn(
          "inline-flex items-center gap-1 text-[10px] text-violet-300",
          canExpand && "hover:text-violet-200 cursor-pointer",
          !canExpand && "cursor-default",
        )}
        disabled={!canExpand}
      >
        <Brain className="h-3 w-3" />
        <span>{finalLabel}</span>
        {canExpand && (
          expanded
            ? <ChevronDown className="h-3 w-3" />
            : <ChevronRight className="h-3 w-3" />
        )}
      </button>
      {expanded && paths && (
        <div className="mt-1 flex flex-wrap gap-1 pl-4">
          {paths.slice(0, 30).map((p) => (
            <code
              key={p}
              className="text-[10px] px-1.5 py-0.5 rounded bg-background/60 border border-border break-all"
            >
              {p}
            </code>
          ))}
          {paths.length > 30 && (
            <span className="text-[10px] text-muted-foreground">
              +{paths.length - 30} more
            </span>
          )}
        </div>
      )}
    </div>
  );
}
