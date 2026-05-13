"use client";

/**
 * Graph layout via dagre.
 *
 * React Flow doesn't position nodes automatically — we have to set
 * `position: {x, y}` on each. dagre runs a hierarchical layout on the
 * node+edge graph and gives us coordinates that minimize edge crossings.
 *
 * Sizing
 * ------
 * dagre needs explicit width/height per node to lay them out without
 * overlap. We use fixed sizes that match the React Flow node components'
 * actual rendered dimensions. If the node CSS changes meaningfully,
 * update NODE_WIDTH / NODE_HEIGHT here too.
 *
 * Direction
 * ---------
 * "LR" = left-to-right. Services land in the leftmost rank and files
 * cascade right based on import depth. This matches the architecture-
 * diagram visual style the user is going for.
 */

import dagre from "@dagrejs/dagre";
import { Position, type Edge, type Node } from "reactflow";

const NODE_WIDTH = 220;
const NODE_HEIGHT = 64;

// Dagre tuning. These knobs control how compact the layout is.
//   nodesep — horizontal gap between siblings in same rank
//   ranksep — gap between ranks (columns in LR mode)
//   edgesep — gap reserved between edges going through same area
const RANKSEP = 90;
const NODESEP = 28;
const EDGESEP = 16;

export type LayoutDirection = "LR" | "TB";

export interface LayoutResult {
  nodes: Node[];
  edges: Edge[];
}

export function layoutGraph(
  nodes: Node[],
  edges: Edge[],
  direction: LayoutDirection = "LR",
): LayoutResult {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({
    rankdir: direction,
    nodesep: NODESEP,
    ranksep: RANKSEP,
    edgesep: EDGESEP,
    marginx: 24,
    marginy: 24,
  });

  for (const n of nodes) {
    g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const e of edges) {
    // dagre will skip self-loops automatically, but they shouldn't
    // exist in our data anyway — the extractor filters self-imports.
    g.setEdge(e.source, e.target);
  }

  dagre.layout(g);

  // Apply the computed positions. dagre returns the *center* of each
  // node; React Flow expects the top-left, so subtract half the size.
  const sourcePos = direction === "LR" ? Position.Right : Position.Bottom;
  const targetPos = direction === "LR" ? Position.Left : Position.Top;
  const laidOut: Node[] = nodes.map((n) => {
    const pos = g.node(n.id);
    if (!pos) return n;
    return {
      ...n,
      position: {
        x: pos.x - NODE_WIDTH / 2,
        y: pos.y - NODE_HEIGHT / 2,
      },
      // Pin source/target handle positions to match the direction so
      // edges enter and exit from the correct sides.
      sourcePosition: sourcePos,
      targetPosition: targetPos,
    };
  });

  return { nodes: laidOut, edges };
}

export { NODE_WIDTH, NODE_HEIGHT };
