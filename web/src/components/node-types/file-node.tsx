"use client";

import { memo } from "react";
import { Handle, Position, type NodeProps } from "reactflow";

import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import type { GraphNodeStatus } from "@/lib/types";

/**
 * The node data shape we attach to React Flow nodes of type "file".
 *
 * The graph endpoint gives us status="pending"|"complete" from REST.
 * The "writing" state is set live by the WebSocket layer when a file
 * is being actively generated — see useGraphLive in graph-view.tsx.
 *
 * findingCounts is filled in after the audits endpoint loads, so a
 * brand-new file node will have undefined here. Render gracefully.
 */
export interface FileNodeData {
  label: string; // e.g. "app/main.py"
  group: string; // folder, e.g. "app"
  status: GraphNodeStatus;
  findingCounts?: {
    critical: number;
    warning: number;
    nit: number;
  };
}

/**
 * One file in the graph. Visual conventions:
 *   - rounded card with a colored left bar indicating status
 *   - filename monospaced, folder prefix muted
 *   - severity badges on the right if findings exist
 *   - "writing" status pulses to signal live work
 */
function FileNodeImpl({ data, selected }: NodeProps<FileNodeData>) {
  const { label, group, status, findingCounts } = data;
  const filename = label.includes("/")
    ? label.slice(label.lastIndexOf("/") + 1)
    : label;
  const folderPrefix =
    group === "root" ? "" : `${group}/`;

  const statusBar = cn(
    "absolute left-0 top-0 h-full w-1 rounded-l",
    status === "complete" && "bg-emerald-500",
    status === "writing" && "bg-blue-500 animate-pulse-slow",
    status === "pending" && "bg-zinc-700",
    status === "failed" && "bg-red-500",
  );

  return (
    <div
      className={cn(
        "relative flex h-16 w-[220px] items-center gap-2 rounded-md border bg-card pl-3 pr-2 transition-colors",
        selected
          ? "border-emerald-500 ring-1 ring-emerald-500/40"
          : "border-border hover:border-zinc-600",
        status === "pending" && "opacity-60",
      )}
    >
      <div className={statusBar} aria-hidden />
      <Handle
        type="target"
        position={Position.Left}
        className="!h-2 !w-2 !border-none !bg-zinc-600"
      />
      <div className="min-w-0 flex-1">
        <div className="truncate font-mono text-xs leading-tight">
          {folderPrefix && (
            <span className="text-muted-foreground">{folderPrefix}</span>
          )}
          <span className="font-medium">{filename}</span>
        </div>
        <FindingPills counts={findingCounts} status={status} />
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="!h-2 !w-2 !border-none !bg-zinc-600"
      />
    </div>
  );
}

export const FileNode = memo(FileNodeImpl);

function FindingPills({
  counts,
  status,
}: {
  counts?: FileNodeData["findingCounts"];
  status: GraphNodeStatus;
}) {
  if (status === "pending") {
    return (
      <div className="mt-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
        pending
      </div>
    );
  }
  if (status === "writing") {
    return (
      <div className="mt-0.5 text-[10px] uppercase tracking-wide text-blue-400">
        writing…
      </div>
    );
  }
  if (!counts) {
    return (
      <div className="mt-0.5 text-[10px] uppercase tracking-wide text-muted-foreground">
        no audit yet
      </div>
    );
  }
  const { critical, warning, nit } = counts;
  if (critical + warning + nit === 0) {
    return (
      <div className="mt-0.5 text-[10px] uppercase tracking-wide text-emerald-500">
        clean
      </div>
    );
  }
  return (
    <div className="mt-1 flex gap-1">
      {critical > 0 && (
        <Badge variant="critical" className="h-4 px-1 text-[10px]">
          {critical}
        </Badge>
      )}
      {warning > 0 && (
        <Badge variant="warning" className="h-4 px-1 text-[10px]">
          {warning}
        </Badge>
      )}
      {nit > 0 && (
        <Badge variant="nit" className="h-4 px-1 text-[10px]">
          {nit}
        </Badge>
      )}
    </div>
  );
}
