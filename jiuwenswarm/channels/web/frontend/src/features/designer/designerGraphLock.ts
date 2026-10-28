import { isDesignerPreviewGraph } from './designerBootstrapGraph';
import { allNodesCompleted } from './designerLayerRun';
import { DESIGNER_TERMINAL_RUN_STATUSES, isActiveDesignerRun } from './designerRunView';
import {
  DESIGNER_GRAPH_SOURCE_MANUAL,
  DESIGNER_GRAPH_SOURCE_PROMPT,
  type DesignerExecutionGraph,
  type DesignerNodeState,
} from './executionGraphTypes';

/**
 * The workflow stays read-only from the moment it is built until its first
 * run finishes. A run that is still going (including a pause between layers)
 * keeps the lock. Any finished run (completed, failed or cancelled) unlocks:
 * a single-node or cascade rerun completes without touching the rest of the
 * graph, so nodes an earlier run left failed, cancelled or unrun must not
 * re-lock it. Manual canvases stay editable until a run is actually active.
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
  return !DESIGNER_TERMINAL_RUN_STATUSES.has(params.runStatus || '');
}
