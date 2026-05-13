"use client";

import { memo } from "react";
import {
  Database,
  Github,
  Box,
  CreditCard,
  Cloud,
  Cpu,
  Sparkles,
  HardDrive,
} from "lucide-react";
import { Handle, Position, type NodeProps } from "reactflow";

import { cn } from "@/lib/utils";

/**
 * The node data shape for "external_service" nodes.
 */
export interface ServiceNodeData {
  label: string; // user-supplied display name
  kind: string; // postgres | mysql | redis | railway | vercel | github | stripe | anthropic | openai | other
  hasConfig: boolean; // whether the user provided a config string
}

/**
 * Map service kind → icon + accent color. Lucide icons mostly. Postgres,
 * MySQL, Redis all use the generic Database icon (we don't try to match
 * brand glyphs since lucide doesn't ship them).
 *
 * Color tokens use Tailwind classes so they match the dark theme.
 */
const KIND_VISUALS: Record<
  string,
  { Icon: typeof Database; tint: string; ring: string }
> = {
  postgres: { Icon: Database, tint: "text-sky-400", ring: "ring-sky-500/30" },
  mysql: { Icon: Database, tint: "text-sky-400", ring: "ring-sky-500/30" },
  redis: { Icon: HardDrive, tint: "text-red-400", ring: "ring-red-500/30" },
  railway: { Icon: Cloud, tint: "text-violet-400", ring: "ring-violet-500/30" },
  vercel: { Icon: Cloud, tint: "text-zinc-300", ring: "ring-zinc-500/30" },
  github: { Icon: Github, tint: "text-zinc-300", ring: "ring-zinc-500/30" },
  stripe: { Icon: CreditCard, tint: "text-indigo-400", ring: "ring-indigo-500/30" },
  anthropic: { Icon: Sparkles, tint: "text-orange-400", ring: "ring-orange-500/30" },
  openai: { Icon: Cpu, tint: "text-emerald-400", ring: "ring-emerald-500/30" },
  other: { Icon: Box, tint: "text-zinc-400", ring: "ring-zinc-500/30" },
};

function ServiceNodeImpl({ data, selected }: NodeProps<ServiceNodeData>) {
  const v = KIND_VISUALS[data.kind] ?? KIND_VISUALS.other;
  const Icon = v.Icon;

  return (
    <div
      className={cn(
        "relative flex h-16 w-[220px] items-center gap-3 rounded-lg border bg-card px-3 transition-colors",
        "ring-1",
        v.ring,
        selected
          ? "border-emerald-500 ring-emerald-500/40"
          : "border-border hover:border-zinc-600",
      )}
    >
      <div
        className={cn(
          "flex h-9 w-9 shrink-0 items-center justify-center rounded-md bg-background/60",
        )}
      >
        <Icon className={cn("h-5 w-5", v.tint)} />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate text-sm font-medium">{data.label}</div>
        <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
          {data.kind}
          {data.hasConfig ? " · configured" : ""}
        </div>
      </div>
      <Handle
        type="source"
        position={Position.Right}
        className="!h-2 !w-2 !border-none !bg-zinc-600"
      />
    </div>
  );
}

export const ServiceNode = memo(ServiceNodeImpl);
