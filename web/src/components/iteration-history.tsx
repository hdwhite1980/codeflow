"use client";

import { useState } from "react";
import { ChevronDown, ChevronRight, Clock } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { Iteration } from "@/lib/types";

interface IterationHistoryProps {
  iterations: Iteration[];
  /**
   * Iteration seqs currently in flight (queued or running). The history
   * shows them as "running" with a pulsing indicator even before the
   * outcome record lands.
   */
  inFlightSeqs: Set<number>;
}

/**
 * Compact sidebar listing past iterations. Each row collapses to a
 * one-liner; clicking expands to show changed files and prompt.
 *
 * Designed to take ~280px on the left edge. If there are no iterations
 * (fresh build, never iterated), we render nothing — no point in
 * occupying space for an empty list.
 */
export function IterationHistory({
  iterations,
  inFlightSeqs,
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
          />
        ))}
      </div>
    </div>
  );
}

function IterationRow({
  iteration,
  running,
}: {
  iteration: Iteration;
  running: boolean;
}) {
  const [expanded, setExpanded] = useState(false);
  const isRunning = running || iteration.status === "running";
  const isAutopatch = iteration.autopatch_attempt != null;

  const changeCount =
    iteration.changes_applied.length +
    iteration.new_files_created.length +
    iteration.files_deleted.length;

  return (
    <div
      className={cn(
        "rounded-md border bg-card/60 transition-colors",
        isRunning
          ? "border-blue-700/50"
          : iteration.failed.length > 0
            ? "border-amber-700/40"
            : isAutopatch
              ? "border-violet-700/40"
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
            {isRunning && (
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
          </div>
          <div className="mt-0.5 truncate text-xs text-muted-foreground">
            {iteration.rationale || iteration.prompt}
          </div>
        </div>
      </button>
      {expanded && (
        <div className="border-t border-border px-3 py-2 text-xs">
          {iteration.prompt && (
            <div className="mb-2">
              <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
                Prompt
              </div>
              <div className="mt-0.5 break-words">{iteration.prompt}</div>
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
            <div className="mt-2">
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
          {(iteration.input_tokens > 0 || iteration.output_tokens > 0) && (
            <div className="mt-2 flex items-center gap-1 text-[10px] text-muted-foreground">
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
    <div className="mt-2">
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
