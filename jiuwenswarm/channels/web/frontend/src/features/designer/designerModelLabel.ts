// Design has no model switch inside the canvas: the chat dock shows the one
// model the design session actually runs on. Its own choice wins while it
// still exists in the catalog, otherwise the Settings default, otherwise the
// first usable model, so the label cannot disagree with the model the backend
// is sent. `models.list` answers with one all-empty placeholder entry when
// nothing is configured, so entries without a model name are skipped.

import type { ModelEntry } from '../../types';

function usableDesignModels(models: ModelEntry[] | null | undefined): ModelEntry[] {
  return (Array.isArray(models) ? models : []).filter(
    (model) => String(model?.model_name ?? '').trim() !== '',
  );
}

/**
 * The catalog row the canvas should show. A stable id wins when several rows
 * share ``model_name``; otherwise the shared name, the Settings default, then
 * the first usable row.
 */
export function resolveDesignerDisplayedModel(
  models: ModelEntry[] | null | undefined,
  selectedModelName: string | null,
  defaultModelName: string | null,
  selectedModelId?: string | null,
): ModelEntry | null {
  const usable = usableDesignModels(models);
  if (usable.length === 0) return null;
  const wantedId = String(selectedModelId ?? '').trim();
  if (wantedId) {
    const byId = usable.find((model) => model.model_id === wantedId);
    if (byId) return byId;
  }
  const wanted = selectedModelName || defaultModelName;
  return usable.find((model) => model.alias === wanted || model.model_name === wanted)
    ?? usable.find((model) => (model.alias || model.model_name) === wanted)
    ?? usable[0]
    ?? null;
}

export function designerModelLabel(
  models: ModelEntry[],
  selectedModelName: string | null,
  defaultModelName: string | null,
  selectedModelId?: string | null,
): string {
  const hit = resolveDesignerDisplayedModel(models, selectedModelName, defaultModelName, selectedModelId);
  return String(hit?.alias || hit?.model_name || '').trim();
}
