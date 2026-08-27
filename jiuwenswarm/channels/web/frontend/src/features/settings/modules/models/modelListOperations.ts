import type { ModelEntry } from '../../../../types';

export type ModelDisplayItem = {
  model: ModelEntry;
  index: number;
};

export type ModelDisplayGroup = {
  modelName: string;
  items: ModelDisplayItem[];
};

export function getEditableModels(models: ModelEntry[]): ModelEntry[] {
  return models.filter((model) => model.is_free !== true && model.is_agentos !== true);
}

export function getModelDisplayGroups(models: ModelEntry[]): ModelDisplayGroup[] {
  const editableGroups = new Map<string, ModelDisplayItem[]>();
  models.forEach((model, index) => {
    if (model.is_free === true || model.is_agentos === true) return;
    const items = editableGroups.get(model.model_name) ?? [];
    items.push({ model, index });
    editableGroups.set(model.model_name, items);
  });

  const emittedModelNames = new Set<string>();
  const displayGroups: ModelDisplayGroup[] = [];
  models.forEach((model, index) => {
    if (model.is_free === true || model.is_agentos === true) {
      displayGroups.push({ modelName: model.model_name, items: [{ model, index }] });
      return;
    }
    if (emittedModelNames.has(model.model_name)) return;
    emittedModelNames.add(model.model_name);
    displayGroups.push({ modelName: model.model_name, items: editableGroups.get(model.model_name)! });
  });
  return displayGroups;
}

export function removeEditableModel(models: ModelEntry[], target: ModelEntry): ModelEntry[] {
  if (getEditableModels(models).length <= 1) throw new Error('LAST_EDITABLE_MODEL');
  return models.filter((model) => model !== target);
}

export function promotePrimaryModel(models: ModelEntry[], target: ModelEntry): ModelEntry[] {
  return [
    { ...target, is_default: true },
    ...models
      .filter((candidate) => candidate !== target)
      .map((candidate) =>
        candidate.model_name === target.model_name && candidate.is_agentos !== true
          ? { ...candidate, is_default: false }
          : candidate,
      ),
  ];
}

export function setGroupDefaultModel(models: ModelEntry[], target: ModelEntry): ModelEntry[] {
  const primaryModel = getEditableModels(models)[0];
  if (target.model_name === primaryModel?.model_name) return promotePrimaryModel(models, target);
  return models.map((candidate) =>
    candidate.model_name === target.model_name && candidate.is_agentos !== true
      ? { ...candidate, is_default: candidate === target }
      : candidate,
  );
}
