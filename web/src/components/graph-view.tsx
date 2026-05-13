"use client";

import { useEffect, useMemo, useState } from "react";
import ReactFlow, {
  Background,
  Controls,
  type Edge,
  type Node,
  type NodeMouseHandler,
  Panel,
} from "reactflow";
import "reactflow/dist/style.css";

import { FileNode } from "@/components/node-types/file-node";
import { ServiceNode } from "@/components/node-types/service-node";
import { SidePanel } from "@/components/side-panel";
import { layoutGraph } from "@/lib/layout";
import type {
  AuditVerdict,
  GraphEdge,
  GraphNode,
  GraphResponse,
} from "@/lib/types";

/**
 * The big one. Renders the project as a React Flow graph:
 *   - Service nodes on the left edge
 *   - File nodes in the middle, grouped by folder
 *   - Edges showing imports (grey) and service uses (colored)
 *
 * Selection
 * ---------
 * Click a node → side panel opens with details. Click the canvas (or
 * the panel's close button) → deselect.
 *
 * Layout
 * ------
 * We use dagre with rankdir=LR. The layout is recomputed when the
 * nodes/edges arrays change identity (i.e. when new data arrives over
 * WebSocket). React Flow caches positions internally so existing nodes
 * don't jump on every re-layout — only newly added nodes get placed
 * fresh, and dagre keeps things stable across updates.
 *
 * Live updates
 * ------------
 * This component is the *view*. The container (project-detail-client)
 * owns the data and passes it in. When new ledger_entry events arrive,
 * the container updates its state and React Flow re-renders. We don't
 * subscribe to anything in here.
 */

const NODE_TYPES = {
  file: FileNode,
  external_service: ServiceNode,
};

interface GraphViewProps {
  graph: GraphResponse | null;
  /** Per-file verdicts keyed by file path; used for the side panel and node finding counts. */
  verdictsByPath: Map<string, AuditVerdict[]>;
}

export function GraphView({ graph, verdictsByPath }: GraphViewProps) {
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Convert backend nodes/edges to React Flow's shape and run layout.
  // Recomputed whenever the upstream graph changes — typically on WS
  // event arrival.
  const { rfNodes, rfEdges } = useMemo(
    () => buildFlowGraph(graph, verdictsByPath),
    [graph, verdictsByPath],
  );

  // Translate selection id → the backend GraphNode and its verdicts.
  const selectedBackendNode: GraphNode | null = useMemo(() => {
    if (!selectedId || !graph) return null;
    return graph.nodes.find((n) => n.id === selectedId) ?? null;
  }, [selectedId, graph]);

  const verdictsForSelected: AuditVerdict[] = useMemo(() => {
    if (!selectedBackendNode || selectedBackendNode.type !== "file") return [];
    const path = (selectedBackendNode.data as { path?: string }).path;
    if (!path) return [];
    return verdictsByPath.get(path) ?? [];
  }, [selectedBackendNode, verdictsByPath]);

  const onNodeClick: NodeMouseHandler = (_evt, node) => {
    setSelectedId(node.id);
  };

  // Click the canvas (pane) to deselect.
  const onPaneClick = () => setSelectedId(null);

  if (!graph) {
    return (
      <div className="flex h-[600px] items-center justify-center rounded-md border border-border bg-card/40 text-sm text-muted-foreground">
        Loading graph…
      </div>
    );
  }

  if (rfNodes.length === 0) {
    return (
      <div className="flex h-[600px] items-center justify-center rounded-md border border-border bg-card/40 text-sm text-muted-foreground">
        No nodes to render yet. Files and edges will appear as the build
        progresses.
      </div>
    );
  }

  return (
    <>
      <div className="h-[calc(100vh-12rem)] w-full overflow-hidden rounded-md border border-border bg-card/20">
        <ReactFlow
          nodes={rfNodes}
          edges={rfEdges}
          nodeTypes={NODE_TYPES}
          onNodeClick={onNodeClick}
          onPaneClick={onPaneClick}
          fitView
          fitViewOptions={{ padding: 0.2, includeHiddenNodes: false }}
          minZoom={0.2}
          maxZoom={2.0}
          proOptions={{ hideAttribution: true }}
          // Disable interactions we don't need to keep the UX focused
          // on viewing/selecting. The user is not editing this graph.
          nodesDraggable={false}
          nodesConnectable={false}
          elementsSelectable
        >
          <Background gap={24} size={1} color="rgba(255,255,255,0.05)" />
          <Controls
            showInteractive={false}
            className="!border-border !bg-card !text-foreground"
          />
          <Panel
            position="top-left"
            className="rounded-md border border-border bg-card/80 px-3 py-2 text-xs text-muted-foreground"
          >
            {graph.node_count} nodes · {graph.edge_count} edges
          </Panel>
        </ReactFlow>
      </div>
      <SidePanel
        selectedNode={selectedBackendNode}
        verdictsForNode={verdictsForSelected}
        onClose={() => setSelectedId(null)}
      />
    </>
  );
}

// ---------------------------------------------------------------------------
// Mapping: backend GraphResponse → React Flow nodes/edges with layout.
// ---------------------------------------------------------------------------

function buildFlowGraph(
  graph: GraphResponse | null,
  verdictsByPath: Map<string, AuditVerdict[]>,
): { rfNodes: Node[]; rfEdges: Edge[] } {
  if (!graph) return { rfNodes: [], rfEdges: [] };

  // We display:
  //   - file nodes
  //   - external_service nodes
  // Spec entities and decision records are filtered out — they'd clutter
  // the architectural view. The graph endpoint already filters most of
  // them, but defensive double-check here keeps any future spec entity
  // additions from polluting the canvas.
  const visibleNodes = graph.nodes.filter(
    (n) => n.type === "file" || n.type === "external_service",
  );

  const visibleIds = new Set(visibleNodes.map((n) => n.id));

  const rfNodes: Node[] = visibleNodes.map((n) => {
    if (n.type === "file") {
      const path = (n.data as { path?: string }).path ?? n.label;
      const verdicts = verdictsByPath.get(path) ?? [];
      const findingCounts = summarizeFindings(verdicts);
      return {
        id: n.id,
        type: "file",
        position: { x: 0, y: 0 }, // dagre overwrites
        data: {
          label: n.label,
          group: n.group,
          status: n.status,
          findingCounts: verdicts.length > 0 ? findingCounts : undefined,
        },
      };
    }
    // external_service
    const data = n.data as { kind?: string; label?: string; config?: unknown };
    return {
      id: n.id,
      type: "external_service",
      position: { x: 0, y: 0 },
      data: {
        label: data.label ?? n.label,
        kind: data.kind ?? "other",
        hasConfig: Boolean(data.config),
      },
    };
  });

  // Edges: only render if BOTH endpoints are visible. We filter for two
  // kinds the user actually wants to see:
  //   "imports"       — file → file
  //   "binds_env_var" — file → service (the kind the extractor writes)
  // Other edge kinds (implements, depends_on, satisfies_manifest_item)
  // are noise for the architecture view and are skipped.
  const rfEdges: Edge[] = graph.edges
    .filter(
      (e) =>
        (e.kind === "imports" || e.kind === "binds_env_var") &&
        visibleIds.has(e.source) &&
        visibleIds.has(e.target),
    )
    .map((e) => toFlowEdge(e));

  // Run dagre layout. dagre handles the positioning so the architecture
  // reads cleanly: services on the left, files cascading right.
  const positioned = layoutGraph(rfNodes, rfEdges, "LR");
  return { rfNodes: positioned.nodes, rfEdges: positioned.edges };
}

function toFlowEdge(e: GraphEdge): Edge {
  const isService = e.kind === "binds_env_var";
  return {
    id: e.id,
    source: e.source,
    target: e.target,
    type: "default",
    animated: false,
    style: isService
      ? { stroke: "rgb(99, 102, 241)", strokeWidth: 1.5 }
      : { stroke: "rgb(82, 82, 91)", strokeWidth: 1 },
    label: isService ? "uses" : undefined,
    labelStyle: { fontSize: 10, fill: "rgb(161, 161, 170)" },
    labelBgStyle: { fill: "rgb(24, 24, 27)" },
  };
}

function summarizeFindings(verdicts: AuditVerdict[]): {
  critical: number;
  warning: number;
  nit: number;
} {
  const out = { critical: 0, warning: 0, nit: 0 };
  for (const v of verdicts) {
    for (const f of v.findings) {
      if (f.severity === "critical") out.critical++;
      else if (f.severity === "warning") out.warning++;
      else if (f.severity === "nit") out.nit++;
    }
  }
  return out;
}
