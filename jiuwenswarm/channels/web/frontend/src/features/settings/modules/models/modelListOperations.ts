import type { ModelEntry } from '../../../../types';

/** Matches backend ``LOGIN_MODEL_SOURCE``: login-granted models must not be edited here. */
export const LOGIN_MODEL_SOURCE = 'huawei-maas-login';

export type ModelDisplayItem = {
  model: ModelEntry;
  index: number;
};

export type ModelDisplayGroup = {
  modelName: string;
  items: ModelDisplayItem[];
};

/** Login / campaign models belong in the chat picker, not the settings catalog. */
export function isRuntimeGrantedModel(model: ModelEntry): boolean {
  return model.is_free === true || model.source === LOGIN_MODEL_SOURCE;
}
export function getEditableModels(models: ModelEntry[]): ModelEntry[] {
  return models.filter((model) => !isRuntimeGrantedModel(model) && model.is_agentos !== true);
}

export function getModelDisplayGroups(models: ModelEntry[]): ModelDisplayGroup[] {
  const editableGroups = new Map<string, ModelDisplayItem[]>();
  models.forEach((model, index) => {
    if (isRuntimeGrantedModel(model) || model.is_agentos === true) return;
    const items = editableGroups.get(model.model_name) ?? [];
    items.push({ model, index });
    editableGroups.set(model.model_name, items);
  });

  const emittedModelNames = new Set<string>();
  const displayGroups: ModelDisplayGroup[] = [];
  models.forEach((model, index) => {
    if (isRuntimeGrantedModel(model)) return;
    if (model.is_agentos === true) {
      displayGroups.push({ modelName: model.model_name, items: [{ model, index }] });
      return;
    }
    if (emittedModelNames.has(model.model_name)) return;
    emittedModelNames.add(model.model_name);
    displayGroups.push({ modelName: model.model_name, items: editableGroups.get(model.model_name)! });
  });
  return displayGroups;
}
