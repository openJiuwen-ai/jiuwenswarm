import {
  DESIGNER_NODE_STATUS_COMPLETED,
  DESIGNER_NODE_STATUS_FAILED,
  DESIGNER_NODE_STATUS_PENDING,
  DESIGNER_NODE_STATUS_RUNNING,
  type DesignerExecutionGraph,
  type DesignerNodeState,
} from './executionGraphTypes';

export type DesignerRunPrimaryAction =
  | 'execute'
  | 'continue'
  | 'retry_failed'
  | 'running'
  | 'done';

export type DesignerLayerRunSnapshot = {
  nodeStates: Record<string, DesignerNodeState>;
  currentLayerNodeIds: string[];
  primaryAction: DesignerRunPrimaryAction;
};

export function buildIncomingMap(graph: DesignerExecutionGraph): Map<string, string[]> {
  const incoming = new Map<string, string[]>();
  for (const node of graph.nodes) {
    incoming.set(node.id, []);
  }
  for (const edge of graph.edges) {
    const list = incoming.get(edge.target);
    if (list) {
      list.push(edge.source);
    } else {
      incoming.set(edge.target, [edge.source]);
    }
  }
  return incoming;
}

export function buildOutgoingMap(graph: DesignerExecutionGraph): Map<string, string[]> {
  const outgoing = new Map<string, string[]>();
  for (const node of graph.nodes) {
    outgoing.set(node.id, []);
  }
  for (const edge of graph.edges) {
    const list = outgoing.get(edge.source);
    if (list) {
      list.push(edge.target);
    } else {
      outgoing.set(edge.source, [edge.target]);
    }
  }
  return outgoing;
}

export function initialNodeStates(graph: DesignerExecutionGraph): Record<string, DesignerNodeState> {
  const states: Record<string, DesignerNodeState> = {};
  for (const node of graph.nodes) {
    states[node.id] = { status: DESIGNER_NODE_STATUS_PENDING };
  }
  return states;
}

/** First layer: nodes with no incoming edges. */
export function computeRootLayer(graph: DesignerExecutionGraph): string[] {
  const incoming = buildIncomingMap(graph);
  return graph.nodes
    .map((node) => node.id)
    .filter((nodeId) => (incoming.get(nodeId) ?? []).length === 0);
}

/**
 * Next layer = direct downstream of previous-layer nodes that are not completed yet.
 * Prefer nodes whose parents are all completed (ready), so we don't run blocked nodes early.
 */
export function computeNextLayer(
  graph: DesignerExecutionGraph,
  previousLayerNodeIds: string[],
  nodeStates: Record<string, DesignerNodeState>,
): string[] {
  if (previousLayerNodeIds.length === 0) {
    return computeRootLayer(graph);
  }

  const outgoing = buildOutgoingMap(graph);
  const incoming = buildIncomingMap(graph);
  const candidates = new Set<string>();
  for (const nodeId of previousLayerNodeIds) {
    for (const childId of outgoing.get(nodeId) ?? []) {
      const status = nodeStates[childId]?.status;
      if (status === DESIGNER_NODE_STATUS_COMPLETED) continue;
      candidates.add(childId);
    }
  }

  const ready: string[] = [];
  for (const nodeId of candidates) {
    const parents = incoming.get(nodeId) ?? [];
    const parentsDone = parents.every(
      (parentId) => nodeStates[parentId]?.status === DESIGNER_NODE_STATUS_COMPLETED,
    );
    if (parentsDone) {
      ready.push(nodeId);
    }
  }

  // Stable order: follow graph.nodes declaration order.
  const order = new Map(graph.nodes.map((node, index) => [node.id, index]));
  return ready.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
}

export function layerHasFailedNodes(
  nodeIds: string[],
  nodeStates: Record<string, DesignerNodeState>,
): boolean {
  return nodeIds.some((nodeId) => nodeStates[nodeId]?.status === DESIGNER_NODE_STATUS_FAILED);
}

export function allNodesCompleted(
  graph: DesignerExecutionGraph,
  nodeStates: Record<string, DesignerNodeState>,
): boolean {
  if (graph.nodes.length === 0) return false;
  return graph.nodes.every(
    (node) => nodeStates[node.id]?.status === DESIGNER_NODE_STATUS_COMPLETED,
  );
}

export function derivePrimaryAction(params: {
  isRunning: boolean;
  graph: DesignerExecutionGraph | null;
  nodeStates: Record<string, DesignerNodeState>;
  currentLayerNodeIds: string[];
}): DesignerRunPrimaryAction {
  if (!params.graph || params.graph.nodes.length === 0) {
    return 'execute';
  }
  if (params.isRunning) {
    return 'running';
  }
  if (allNodesCompleted(params.graph, params.nodeStates)) {
    return 'done';
  }
  if (
    params.currentLayerNodeIds.length > 0 &&
    layerHasFailedNodes(params.currentLayerNodeIds, params.nodeStates)
  ) {
    return 'retry_failed';
  }
  if (params.currentLayerNodeIds.length > 0) {
    const next = computeNextLayer(
      params.graph,
      params.currentLayerNodeIds,
      params.nodeStates,
    );
    if (next.length > 0) {
      return 'continue';
    }
  }
  const hasProgress = Object.values(params.nodeStates).some(
    (state) =>
      state.status === DESIGNER_NODE_STATUS_COMPLETED ||
      state.status === DESIGNER_NODE_STATUS_FAILED ||
      state.status === DESIGNER_NODE_STATUS_RUNNING,
  );
  return hasProgress ? 'continue' : 'execute';
}

export function markNodesStatus(
  nodeStates: Record<string, DesignerNodeState>,
  nodeIds: string[],
  status: string,
  extra?: Partial<DesignerNodeState>,
): Record<string, DesignerNodeState> {
  const next = { ...nodeStates };
  const now = Date.now();
  for (const nodeId of nodeIds) {
    const current = next[nodeId] ?? { status: DESIGNER_NODE_STATUS_PENDING };
    next[nodeId] = {
      ...current,
      status,
      ...extra,
      ...(status === DESIGNER_NODE_STATUS_RUNNING
        ? { started_at: now, completed_at: null, error: null }
        : {}),
      ...(status === DESIGNER_NODE_STATUS_COMPLETED
        ? { completed_at: now, error: null }
        : {}),
      ...(status === DESIGNER_NODE_STATUS_FAILED ? { completed_at: now } : {}),
    };
  }
  return next;
}
