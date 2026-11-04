// Design has no model switch inside the canvas: the chat dock shows the one
// model the design session actually runs on. Its own choice wins while it
// still exists in the catalog, otherwise the Settings default, otherwise the
// first usable model, so the label cannot disagree with the model the backend
// is sent. `models.list` answers with one all-empty placeholder entry when
// nothing is configured, so entries without a model name are skipped.

import type { ModelEntry } from '../../types';

export function designerModelLabel(
  models: ModelEntry[],
  selectedModelName: string | null,
  defaultModelName: string | null,
): string {
  const usable = (Array.isArray(models) ? models : []).filter(
    (model) => String(model?.model_name ?? '').trim() !== '',
  );
  if (usable.length === 0) return '';
  const wanted = selectedModelName || defaultModelName;
  const hit = usable.find((model) => model.alias === wanted || model.model_name === wanted)
    ?? usable.find((model) => (model.alias || model.model_name) === wanted)
    ?? usable[0];
  return String(hit?.alias || hit?.model_name || '').trim();
}