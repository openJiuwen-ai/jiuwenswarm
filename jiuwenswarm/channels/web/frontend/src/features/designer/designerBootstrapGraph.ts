import {
  DESIGNER_GRAPH_SCHEMA_VERSION,
  DESIGNER_GRAPH_SOURCE_PROMPT,
  type DesignerExecutionGraph,
} from './executionGraphTypes';

export const DESIGNER_PREVIEW_GRAPH_ID = 'preview_bootstrap';

export function isDesignerPreviewGraph(
  graph: Pick<DesignerExecutionGraph, 'graph_id'> | null | undefined,
): boolean {
  const id = String(graph?.graph_id || '');
  return id === DESIGNER_PREVIEW_GRAPH_ID || id.startsWith('preview_');
}

/**
 * Placeholder while Leader is composing. Empty on purpose — a skeleton graph
 * looks like a finished workflow and is easy to mistake for the real canvas.
 */
export function graphForBootstrapThinking(prompt = ''): DesignerExecutionGraph {
  const now = Date.now();
  const promptText = prompt.trim();
  return {
    schema_version: DESIGNER_GRAPH_SCHEMA_VERSION,
    graph_id: DESIGNER_PREVIEW_GRAPH_ID,
    project_id: '',
    title: promptText.slice(0, 80) || 'Design',
    description: promptText,
    source: DESIGNER_GRAPH_SOURCE_PROMPT,
    nodes: [],
    edges: [],
    metadata: { bootstrap: 'designer.graph.bootstrap.thinking' },
    created_at: now,
    updated_at: now,
  };
}
