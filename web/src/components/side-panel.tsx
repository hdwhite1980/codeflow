"use client";

import { X } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { FindingList } from "@/components/finding-list";
import { providerLabel } from "@/lib/format";
import { cn } from "@/lib/utils";
import type {
  AuditVerdict,
  GraphNode,
  ServiceConfig,
} from "@/lib/types";

interface SidePanelProps {
  /** The node the user clicked, or null when nothing is selected. */
  selectedNode: GraphNode | null;
  /** Verdicts for the selected file (filtered by caller). Empty/missing when not a file. */
  verdictsForNode: AuditVerdict[];
  onClose: () => void;
}

/**
 * Slide-in panel from the right edge. Width is roughly 30% of viewport
 * on desktop; on narrow screens it goes full-width.
 *
 * Renders different content per node type:
 *   - file: OpenAI verdict | Gemini verdict, stacked
 *   - external_service: kind, label, config (with secret masking)
 *   - decision_record: rationale text
 *   - everything else: a generic key/value dump of `data`
 *
 * Closing the panel deselects the node — the parent (graph-view) is
 * the source of truth for selection state.
 */
export function SidePanel({
  selectedNode,
  verdictsForNode,
  onClose,
}: SidePanelProps) {
  // We render the panel as a sliding overlay rather than mounting/
  // unmounting it. That way the slide animation works cleanly and
  // keyboard focus management is simpler.
  const open = selectedNode !== null;

  return (
    <aside
      className={cn(
        "fixed right-0 top-0 z-40 h-screen w-full max-w-[420px] transform border-l border-border bg-card shadow-2xl transition-transform duration-200 ease-out",
        open ? "translate-x-0" : "translate-x-full",
      )}
      aria-hidden={!open}
    >
      <div className="flex h-14 items-center justify-between border-b border-border px-4">
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-medium">
            {selectedNode?.label ?? ""}
          </div>
          <div className="text-[10px] uppercase tracking-wide text-muted-foreground">
            {selectedNode?.type.replace(/_/g, " ")}
          </div>
        </div>
        <Button
          variant="ghost"
          size="icon"
          onClick={onClose}
          aria-label="Close details"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
      <div className="h-[calc(100vh-3.5rem)] overflow-y-auto p-4">
        {selectedNode && renderNodeContent(selectedNode, verdictsForNode)}
      </div>
    </aside>
  );
}

function renderNodeContent(
  node: GraphNode,
  verdicts: AuditVerdict[],
): React.ReactNode {
  switch (node.type) {
    case "file":
      return <FileDetail node={node} verdicts={verdicts} />;
    case "external_service":
      return <ServiceDetail node={node} />;
    case "decision_record":
      return <DecisionDetail node={node} />;
    default:
      return <GenericDetail node={node} />;
  }
}

// ---------------------------------------------------------------------------
// File detail: per-auditor findings stacked.
// ---------------------------------------------------------------------------

function FileDetail({
  node,
  verdicts,
}: {
  node: GraphNode;
  verdicts: AuditVerdict[];
}) {
  if (node.status === "pending") {
    return (
      <div className="rounded-md border border-border bg-background/40 p-4 text-sm text-muted-foreground">
        This file is in the spec but hasn&apos;t been generated yet. Audit
        verdicts will appear after the build runs.
      </div>
    );
  }
  if (verdicts.length === 0) {
    return (
      <div className="rounded-md border border-border bg-background/40 p-4 text-sm text-muted-foreground">
        Auditors haven&apos;t reviewed this file yet. Findings will appear
        as OpenAI and Gemini complete their passes.
      </div>
    );
  }
  return (
    <div className="space-y-4">
      {verdicts.map((v) => (
        <section key={v.artifact_key} className="space-y-2">
          <div className="flex items-center justify-between">
            <Badge variant="provider">{providerLabel(v.auditor)}</Badge>
            <span className="text-xs text-muted-foreground">
              {v.finding_count}{" "}
              {v.finding_count === 1 ? "finding" : "findings"}
            </span>
          </div>
          <FindingList findings={v.findings} parseError={v.parse_error} />
        </section>
      ))}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Service detail: kind, label, optional config (with light masking).
// ---------------------------------------------------------------------------

function ServiceDetail({ node }: { node: GraphNode }) {
  const data = node.data as Partial<ServiceConfig> & { kind?: string };
  const config = data.config ?? null;
  return (
    <div className="space-y-4">
      <KeyValue label="Kind" value={data.kind ?? "unknown"} />
      <KeyValue label="Label" value={data.label ?? "(none)"} />
      {config ? (
        <div className="space-y-1">
          <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
            Config
          </div>
          <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-border bg-background/40 p-3 text-xs text-zinc-300">
            {maskSecret(config)}
          </pre>
          <p className="text-[10px] text-muted-foreground">
            Secrets after `=` are masked. The unmasked value is still on
            the backend and used to draw edges to files that reference
            this service.
          </p>
        </div>
      ) : (
        <p className="text-xs text-muted-foreground">
          No config provided. Edges to this service won&apos;t be
          auto-detected — add a config string (env var name or URL) when
          creating future projects so files can be linked automatically.
        </p>
      )}
    </div>
  );
}

function KeyValue({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
        {label}
      </div>
      <div className="mt-1 break-words text-sm">{value}</div>
    </div>
  );
}

/**
 * Mask anything after the first `=` in a config string. Crude but
 * useful — `DATABASE_URL=postgres://user:pw@host/db` becomes
 * `DATABASE_URL=****`. Doesn't try to be clever about JSON or other
 * shapes since the config field is documented as opaque metadata.
 */
function maskSecret(s: string): string {
  return s.replace(/=([^\s]+)/g, "=****");
}

// ---------------------------------------------------------------------------
// Decision record detail — these are build/audit lifecycle entries
// that we render rarely (right now they're not in the graph because
// we don't expose them as nodes). Reserved for later.
// ---------------------------------------------------------------------------

function DecisionDetail({ node }: { node: GraphNode }) {
  const rationale = (node.data as { rationale?: string }).rationale;
  return (
    <div className="space-y-3 text-sm">
      {rationale && (
        <div className="rounded-md border border-border bg-background/40 p-3">
          {rationale}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Generic fallback — show the raw data payload as JSON.
// ---------------------------------------------------------------------------

function GenericDetail({ node }: { node: GraphNode }) {
  return (
    <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-md border border-border bg-background/40 p-3 text-xs">
      {JSON.stringify(node.data, null, 2)}
    </pre>
  );
}
