import type {
  BeamSearchEdge,
  BeamSearchNode,
  BeamSearchProgress,
} from '../../types/beamSearch';

export interface BeamTreeNodeEntry {
  kind: 'node';
  node: BeamSearchNode;
  children: BeamTreeEntry[];
}

export interface BeamTreeMergeEntry {
  kind: 'merge';
  node: BeamSearchNode;
}

export type BeamTreeEntry = BeamTreeNodeEntry | BeamTreeMergeEntry;

export interface BeamTreeModel {
  roots: BeamTreeNodeEntry[];
  nodeCount: number;
}

function compareNodes(left: BeamSearchNode, right: BeamSearchNode): number {
  return (left.label || left.id).localeCompare(right.label || right.id) ||
    left.id.localeCompare(right.id);
}

function compareEdges(
  left: BeamSearchEdge,
  right: BeamSearchEdge,
  nodes: Map<string, BeamSearchNode>
): number {
  const leftNode = nodes.get(left.target);
  const rightNode = nodes.get(right.target);
  if (leftNode && rightNode) {
    const nodeOrder = compareNodes(leftNode, rightNode);
    if (nodeOrder !== 0) return nodeOrder;
  }
  return left.target.localeCompare(right.target) || left.source.localeCompare(right.source);
}

export function buildBeamTree(progress: BeamSearchProgress): BeamTreeModel {
  const nodes = new Map<string, BeamSearchNode>();
  for (const node of progress.graph.nodes) {
    if (node?.id && !nodes.has(node.id)) nodes.set(node.id, node);
  }

  const outgoing = new Map<string, BeamSearchEdge[]>();
  const incomingCount = new Map<string, number>();
  const edgeKeys = new Set<string>();
  for (const edge of progress.graph.edges) {
    if (!nodes.has(edge.source) || !nodes.has(edge.target) || edge.source === edge.target) {
      continue;
    }
    const edgeKey = `${edge.source}\u0000${edge.target}`;
    if (edgeKeys.has(edgeKey)) continue;
    edgeKeys.add(edgeKey);
    const sourceEdges = outgoing.get(edge.source) ?? [];
    sourceEdges.push(edge);
    outgoing.set(edge.source, sourceEdges);
    incomingCount.set(edge.target, (incomingCount.get(edge.target) ?? 0) + 1);
  }
  for (const edges of outgoing.values()) {
    edges.sort((left, right) => compareEdges(left, right, nodes));
  }

  const sortedNodes = [...nodes.values()].sort(compareNodes);
  const seedIds = sortedNodes
    .filter((node) => node.seed === true || node.status === 'seed')
    .map((node) => node.id);
  const seedIdSet = new Set(seedIds);
  const rootIds = [
    ...seedIds,
    ...sortedNodes
      .filter((node) => !seedIdSet.has(node.id) && !incomingCount.has(node.id))
      .map((node) => node.id),
  ];
  const reservedRoots = new Set(rootIds);
  const claimed = new Set<string>();

  const visit = (
    nodeId: string,
    active: Set<string>,
    allowReservedRoot = false
  ): BeamTreeEntry | null => {
    const node = nodes.get(nodeId);
    if (!node) return null;
    if (active.has(nodeId) || claimed.has(nodeId) ||
      (!allowReservedRoot && reservedRoots.has(nodeId))) {
      return { kind: 'merge', node };
    }

    claimed.add(nodeId);
    const nextActive = new Set(active);
    nextActive.add(nodeId);
    const children = (outgoing.get(nodeId) ?? [])
      .map((edge) => visit(edge.target, nextActive))
      .filter((entry): entry is BeamTreeEntry => entry !== null);
    return { kind: 'node', node, children };
  };

  const roots: BeamTreeNodeEntry[] = [];
  const appendRoot = (nodeId: string) => {
    if (claimed.has(nodeId)) return;
    const entry = visit(nodeId, new Set(), true);
    if (entry?.kind === 'node') roots.push(entry);
  };
  rootIds.forEach(appendRoot);
  sortedNodes.forEach((node) => appendRoot(node.id));

  return { roots, nodeCount: nodes.size };
}
