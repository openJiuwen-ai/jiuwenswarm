import { isDesignerPreviewGraph } from './designerBootstrapGraph';
import { allNodesCompleted } from './designerLayerRun';
import { isActiveDesignerRun } from './designerRunView';
import {
  DESIGNER_GRAPH_SOURCE_MANUAL,
  DESIGNER_GRAPH_SOURCE_PROMPT,
  DESIGNER_NODE_STATUS_COMPLETED,
  type DesignerExecutionGraph,
  type DesignerNodeState,
} from './executionGraphTypes';

/**
 * The workflow stays read-only from the moment it is built until every node
 * has finished. A run that is still going (including a pause between layers)
 * keeps the lock. A failed or cancelled attempt unlocks so the graph can be
 * fixed. Nodes added after a completed run do not re-lock it. Manual canvases
 * stay editable until a run is actually active.
 */
export function isDesignerGraphEditingLocked(params: {
  bootstrapInProgress: boolean;
  graph: DesignerExecutionGraph | null | undefined;
  nodeStates: Record<string, DesignerNodeState>;
  runStatus?: string | null;
}): boolean {
  if (params.bootstrapInProgress) return true;
  const graph = params.graph;
  if (!graph) return false;
  if (isDesignerPreviewGraph(graph)) return true;
  if (isActiveDesignerRun(params.runStatus || undefined)) return true;
  if (graph.source === DESIGNER_GRAPH_SOURCE_MANUAL) return false;
  if (graph.source !== DESIGNER_GRAPH_SOURCE_PROMPT) return false;
  if (graph.nodes.length === 0) return false;
  if (allNodesCompleted(graph, params.nodeStates)) return false;
  // Nodes added after a completed run have no state yet; they are new work
  // for the user to wire up, not unfinished output of that run.
  if (params.runStatus === 'completed' && completedExceptUnrun(graph, params.nodeStates)) {
    return false;
  }
  // A finished attempt that did not complete every node can be edited again.
  return params.runStatus !== 'failed' && params.runStatus !== 'cancelled';
}

function completedExceptUnrun(
  graph: DesignerExecutionGraph,
  nodeStates: Record<string, DesignerNodeState>,
): boolean {
  return graph.nodes.every((node) => {
    const status = nodeStates[node.id]?.status;
    return !status || status === DESIGNER_NODE_STATUS_COMPLETED;
  });
}
