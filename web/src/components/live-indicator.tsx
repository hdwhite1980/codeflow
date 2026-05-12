"use client";

import { cn } from "@/lib/utils";
import type { StreamState } from "@/lib/ws";

/**
 * Visual indicator of WebSocket connection state.
 *
 *  - "open":       solid green pulse — events are flowing
 *  - "connecting": amber pulse       — initial connect or reconnect
 *  - "closed":     grey static dot   — disabled or terminally failed
 *
 * Sits inline next to the page title so the user can tell at a glance
 * whether they're seeing live data or a snapshot.
 */
export function LiveIndicator({ state }: { state: StreamState }) {
  const label =
    state === "open"
      ? "Live"
      : state === "connecting"
        ? "Connecting"
        : "Disconnected";

  const dotColor =
    state === "open"
      ? "bg-emerald-500"
      : state === "connecting"
        ? "bg-amber-500"
        : "bg-zinc-600";

  const pulse = state === "open" || state === "connecting";

  return (
    <div className="inline-flex items-center gap-2 text-xs text-muted-foreground">
      <span className="relative inline-flex h-2 w-2">
        {pulse && (
          <span
            className={cn(
              "absolute inline-flex h-full w-full rounded-full opacity-75 animate-ping",
              dotColor,
            )}
          />
        )}
        <span
          className={cn(
            "relative inline-flex h-2 w-2 rounded-full",
            dotColor,
          )}
        />
      </span>
      <span>{label}</span>
    </div>
  );
}
