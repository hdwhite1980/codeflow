"use client";

/**
 * useProjectStream — React hook that subscribes to live events for one
 * project via the FastAPI WebSocket endpoint.
 *
 * Design notes
 * ------------
 *  - Auto-reconnect with exponential backoff: if the WS drops, we
 *    re-open with delays of 1s, 2s, 4s, 8s, 16s (cap), with jitter.
 *    Live data is best-effort; the REST endpoints remain the source
 *    of truth and the caller refetches on reconnect.
 *
 *  - We don't buffer events. If the caller's render loop is slow, the
 *    queue inside the browser's WS implementation absorbs the lag.
 *    Realistic event rate is < 5/sec — well within browser handling.
 *
 *  - We expose connection state ("connecting" | "open" | "closed")
 *    so the UI can show a "live" indicator.
 *
 *  - On unmount, we close cleanly and stop reconnect attempts.
 */

import { useEffect, useRef, useState } from "react";
import { projectWebsocketUrl } from "./api";
import type { WSEvent } from "./types";

export type StreamState = "connecting" | "open" | "closed";

export interface ProjectStreamHookOptions {
  /**
   * Whether to maintain the connection. Pass `false` (e.g. for completed
   * projects) to skip opening the WS. Useful for finished builds where
   * live updates are no longer expected.
   */
  enabled?: boolean;
  /**
   * Called for every event received. Should be stable across renders
   * (useCallback or module-level fn) — we install it once per mount.
   */
  onEvent: (event: WSEvent) => void;
}

const MAX_BACKOFF_MS = 16_000;
const BASE_BACKOFF_MS = 1_000;

export function useProjectStream(
  projectId: string,
  options: ProjectStreamHookOptions,
) {
  const { enabled = true, onEvent } = options;
  const [state, setState] = useState<StreamState>("closed");
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttemptRef = useRef(0);
  const closedByUserRef = useRef(false);
  // Keep latest onEvent in a ref so we don't re-open on every render.
  const onEventRef = useRef(onEvent);
  onEventRef.current = onEvent;

  useEffect(() => {
    if (!enabled || !projectId) return;

    closedByUserRef.current = false;
    reconnectAttemptRef.current = 0;

    const connect = () => {
      setState("connecting");
      let ws: WebSocket;
      try {
        ws = new WebSocket(projectWebsocketUrl(projectId));
      } catch (err) {
        // URL construction can throw if NEXT_PUBLIC_API_URL is missing.
        // Surface clearly and stop trying.
        console.error("[useProjectStream] failed to construct WS URL:", err);
        setState("closed");
        return;
      }

      wsRef.current = ws;

      ws.onopen = () => {
        reconnectAttemptRef.current = 0;
        setState("open");
      };

      ws.onmessage = (msgEv) => {
        try {
          const parsed = JSON.parse(msgEv.data) as WSEvent;
          onEventRef.current(parsed);
        } catch (err) {
          // Don't blow up the connection on a malformed frame; just log.
          console.warn("[useProjectStream] failed to parse frame:", err);
        }
      };

      ws.onerror = () => {
        // We don't have much actionable info from onerror in browsers;
        // onclose is where we react. This is just for visibility.
        // console.warn("[useProjectStream] WS error");
      };

      ws.onclose = () => {
        wsRef.current = null;
        setState("closed");
        if (closedByUserRef.current) return;

        // Exponential backoff with up to 25% jitter.
        const attempt = reconnectAttemptRef.current;
        const delay = Math.min(
          BASE_BACKOFF_MS * 2 ** attempt,
          MAX_BACKOFF_MS,
        );
        const jittered = delay + Math.random() * delay * 0.25;
        reconnectAttemptRef.current = attempt + 1;

        reconnectTimerRef.current = setTimeout(() => {
          reconnectTimerRef.current = null;
          if (!closedByUserRef.current) connect();
        }, jittered);
      };
    };

    connect();

    return () => {
      closedByUserRef.current = true;
      if (reconnectTimerRef.current !== null) {
        clearTimeout(reconnectTimerRef.current);
        reconnectTimerRef.current = null;
      }
      const ws = wsRef.current;
      wsRef.current = null;
      if (ws && ws.readyState !== WebSocket.CLOSED) {
        ws.close();
      }
    };
  }, [projectId, enabled]);

  return state;
}
