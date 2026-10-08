import type {
  DesignerExecutionGraph,
  DesignerGraphEdge,
  DesignerGraphNode,
  NodeLayout,
} from './executionGraphTypes';
import {
  DESIGNER_CANVAS_NODE_HEIGHT,
  DESIGNER_CANVAS_NODE_WIDTH,
  resolvedNodeCanvasSize,
} from './designerCanvasNodes';

export { resolvedNodeCanvasSize };

/** Minimal React Flow node shape used by the adapter (no @xyflow/react dependency). */
export type DesignerReactFlowNode = {
  id: string;
  type: string;
  position: { x: number; y: number };
  data: {
    label: string;
    nodeType: string;
    config: Record<string, unknown>;
    layout: NodeLayout;
    outputRef: DesignerGraphNode['output_ref'];
  };
  style?: {
    width?: number;
    height?: number;
  };
  /** Declared size. React Flow hides a node until width/height or measured size exists. */
  width?: number;
  height?: number;
  initialWidth?: number;
  initialHeight?: number;
};

/** Minimal React Flow edge shape used by the adapter. */
export type DesignerReactFlowEdge = {
  id: string;
  source: string;
  target: string;
  type?: string;
  label?: string;
};

export type DesignerReactFlowGraph = {
  nodes: DesignerReactFlowNode[];
  edges: DesignerReactFlowEdge[];
};

const DEFAULT_NODE_WIDTH = DESIGNER_CANVAS_NODE_WIDTH;
const DEFAULT_NODE_HEIGHT = DESIGNER_CANVAS_NODE_HEIGHT;

function layoutPosition(layout: NodeLayout | undefined): { x: number; y: number } {
  return {
    x: typeof layout?.x === 'number' ? layout.x : 0,
    y: typeof layout?.y === 'number' ? layout.y : 0,
  };
}

export function toReactFlowGraph(graph: DesignerExecutionGraph): DesignerReactFlowGraph {
  const nodes: DesignerReactFlowNode[] = graph.nodes.map((node) => {
    const size = resolvedNodeCanvasSize(node);
    return {
      id: node.id,
      type: node.type,
      position: layoutPosition(node.layout),
      style: size,
      width: size.width,
      height: size.height,
      initialWidth: size.width,
      initialHeight: size.height,
      data: {
        label: node.label,
        nodeType: node.type,
        config: (node.config ?? {}) as Record<string, unknown>,
        layout: node.layout ?? {},
        outputRef: node.output_ref ?? null,
      },
    };
  });

  const edges: DesignerReactFlowEdge[] = graph.edges.map((edge) => ({
    id: edge.id,
    source: edge.source,
    target: edge.target,
    type: 'designer',
    label: edge.label,
  }));

  return { nodes, edges };
}

function mergeLayout(
  existing: NodeLayout | undefined,
  position: { x: number; y: number },
  style: DesignerReactFlowNode['style'],
): NodeLayout {
  return {
    x: position.x,
    y: position.y,
    width: style?.width ?? existing?.width ?? DEFAULT_NODE_WIDTH,
    height: style?.height ?? existing?.height ?? DEFAULT_NODE_HEIGHT,
  };
}

export function fromReactFlowGraph(
  reactFlow: DesignerReactFlowGraph,
  domainGraph: DesignerExecutionGraph,
): DesignerExecutionGraph {
  const domainNodesById = new Map(domainGraph.nodes.map((node) => [node.id, node]));
  const nodes: DesignerGraphNode[] = reactFlow.nodes.map((rfNode) => {
    const existing = domainNodesById.get(rfNode.id);
    const data = rfNode.data ?? {
      label: rfNode.id,
      nodeType: rfNode.type,
      config: {},
      layout: {},
      outputRef: null,
    };
    return {
      id: rfNode.id,
      type: (existing?.type ?? data.nodeType ?? rfNode.type) as DesignerGraphNode['type'],
      label: data.label ?? existing?.label ?? rfNode.id,
      config: (data.config ?? existing?.config ?? {}) as DesignerGraphNode['config'],
      layout: mergeLayout(existing?.layout, rfNode.position, rfNode.style),
      output_ref: data.outputRef ?? existing?.output_ref ?? null,
    };
  });

  const domainEdgesById = new Map(domainGraph.edges.map((edge) => [edge.id, edge]));
  const edges: DesignerGraphEdge[] = reactFlow.edges.map((rfEdge) => {
    const existing = domainEdgesById.get(rfEdge.id);
    return {
      id: rfEdge.id,
      source: rfEdge.source,
      target: rfEdge.target,
      kind: existing?.kind,
      label: rfEdge.label ?? existing?.label,
    };
  });

  return {
    ...domainGraph,
    nodes,
    edges,
    updated_at: Date.now(),
  };
}
