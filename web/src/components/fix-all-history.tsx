"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Wand2 } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { FixAllPass } from "@/lib/types";

interface FixAllHistoryProps {
  passes: FixAllPass[];
}

/**
 * Compact list of past fix-all passes, with the report expanded
 * inline. Rendered above (or next to) the iteration history so the
 * user can see what each manual fix-all attempt accomplished.
 */
export function FixAllHistory({ passes }: FixAllHistoryProps) {
  if (passes.length === 0) return null;
  return (
    <div className="space-y-2">
      <div className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Fix-all passes
      </div>
      <div className="space-y-1.5">
        {passes.map((p) => (
          <FixAllRow key={p.seq} pass={p} />
        ))}
      </div>
    </div>
  );
}

function FixAllRow({ pass }: { pass: FixAllPass }) {
  const [expanded, setExpanded] = useState(
    // Auto-expand the most recent complete pass so the user sees
    // the report without clicking.
    pass.status === "complete",
  );
  const isRunning = pass.status === "running";
  const isClean = !isRunning && pass.post_count === 0;

  return (
    <div
      className={cn(
        "rounded-md border bg-card/60 transition-colors",
        isRunning
          ? "border-violet-700/50"
          : isClean
            ? "border-emerald-700/40"
            : "border-border",
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
        <Wand2 className="mt-0.5 h-3 w-3 shrink-0 text-violet-400" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <span className="text-xs font-medium tabular-nums">
              Pass #{pass.seq}
            </span>
            {isRunning && (
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
      {expanded && pass.report && (
        <div className="border-t border-border px-3 py-2 text-xs">
          <pre className="whitespace-pre-wrap break-words font-mono text-[11px] leading-relaxed text-muted-foreground">
            {pass.report}
          </pre>
        </div>
      )}
    </div>
  );
}
