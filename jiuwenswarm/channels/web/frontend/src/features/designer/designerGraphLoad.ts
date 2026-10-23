/** Pick which Designer graph to open after a project list/get. */

import type { DesignerExecutionGraph, DesignerGraphSummary } from './executionGraphTypes';

export const DESIGNER_LAST_GRAPH_STORAGE_KEY = 'jiuwenswarm_designer_last_graph_id';

function isPreviewGraphId(graphId: string): boolean {
  return graphId === 'preview_bootstrap' || graphId.startsWith('preview_');
}

export function readLastDesignerGraphId(): string {
  if (typeof window === 'undefined') return '';
  try {
    return String(window.localStorage.getItem(DESIGNER_LAST_GRAPH_STORAGE_KEY) || '').trim();
  } catch {
    return '';
  }
}

export function rememberDesignerGraphId(graphId: string | null | undefined): void {
  const id = String(graphId ?? '').trim();
  if (typeof window === 'undefined') return;
  try {
    if (!id || isPreviewGraphId(id)) {
      window.localStorage.removeItem(DESIGNER_LAST_GRAPH_STORAGE_KEY);
      return;
    }
    window.localStorage.setItem(DESIGNER_LAST_GRAPH_STORAGE_KEY, id);
  } catch {
    // Storage can be unavailable in private or restricted contexts.
  }
}

export function uniqueDesignerGraphIds(ids: Array<string | null | undefined>): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of ids) {
    const id = String(raw ?? '').trim();
    if (!id || seen.has(id)) continue;
    seen.add(id);
    out.push(id);
  }
  return out;
}

export function resolveDesignerGraphToLoad(input: {
  currentId?: string | null;
  isPreview?: boolean;
  listedIds: string[];
  lastId?: string | null;
}): string | null {
  const listedIds = uniqueDesignerGraphIds(input.listedIds);
  const currentId = String(input.currentId ?? '').trim();
  if (currentId && !input.isPreview && !isPreviewGraphId(currentId)) {
    return currentId;
  }
  const lastId = String(
    input.lastId === undefined ? readLastDesignerGraphId() : input.lastId ?? '',
  ).trim();
  if (lastId && !isPreviewGraphId(lastId)) {
    return lastId;
  }
  return listedIds[0] ?? null;
}

export function summariesFromGraphList(payload: {
  graphs?: DesignerExecutionGraph[];
  summaries?: DesignerGraphSummary[];
}): DesignerGraphSummary[] {
  const fromSummaries = (payload.summaries || []).filter((item) =>
    String(item?.graph_id || '').trim(),
  );
  if (fromSummaries.length > 0) {
    return fromSummaries;
  }
  return (payload.graphs || [])
    .map((graph) => ({
      graph_id: String(graph.graph_id || '').trim(),
      project_id: String(graph.project_id || '').trim(),
      title: String(graph.title || graph.graph_id || '').trim(),
      updated_at: graph.updated_at,
      has_video: Boolean(
        graph.nodes?.some((node) => node.output_ref?.kind === 'video' && node.output_ref.uri),
      ),
    }))
    .filter((item) => item.graph_id);
}
