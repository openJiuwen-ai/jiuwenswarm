import type { ModelEntry } from "../../types";

export interface ModelIdentity {
  originIndex?: number;
  fallbackIndex: number;
}

interface AgentModelBinding {
  provider: string;
  api_base: string;
  api_key: string;
  model: string;
}

interface AgentWithModelBinding {
  model: AgentModelBinding;
}

export function findModelIndex(models: ModelEntry[], identity: ModelIdentity): number {
  if (identity.originIndex !== undefined) {
    const persistedIndex = models.findIndex((model) => model.origin_index === identity.originIndex);
    return persistedIndex;
  }
  return identity.fallbackIndex >= 0 && identity.fallbackIndex < models.length
    ? identity.fallbackIndex
    : -1;
}

function modelNonAuthFieldsEqual(left: ModelEntry, right: ModelEntry): boolean {
  return (left.alias ?? "") === (right.alias ?? "")
    && (left.reasoning_level ?? "") === (right.reasoning_level ?? "")
    && left.is_default === right.is_default
    && (left.temperature ?? 0.95) === (right.temperature ?? 0.95)
    && (left.timeout ?? 1800) === (right.timeout ?? 1800);
}

export function modelEntriesEqual(left: ModelEntry, right: ModelEntry): boolean {
  return left.model_name === right.model_name
    && left.api_base === right.api_base
    && left.api_key === right.api_key
    && left.model_provider === right.model_provider
    && modelNonAuthFieldsEqual(left, right);
}

export function patchModelSnapshot(
  models: ModelEntry[],
  identity: ModelIdentity,
  patch: Partial<ModelEntry>,
): ModelEntry[] {
  const targetIndex = findModelIndex(models, identity);
  if (targetIndex < 0) return models;

  const nextModels = [...models];
  nextModels[targetIndex] = { ...nextModels[targetIndex], ...patch };
  return nextModels;
}

export function preserveConfiguredModelName(
  currentModelName: string,
  discoveredModelIds: string[],
): string {
  const configuredModelName = currentModelName.trim();
  if (configuredModelName) return configuredModelName;

  return discoveredModelIds
    .map((modelId) => modelId.trim())
    .find(Boolean) ?? "";
}

export function syncAgentsWithModelChanges<T extends AgentWithModelBinding>(
  agents: T[],
  previousModels: ModelEntry[],
  nextModels: ModelEntry[],
): T[] {
  let changed = false;
  const nextAgents = agents.map((agent) => {
    const previousModelIndex = previousModels.findIndex(
      (model) => model.model_name === agent.model.model
        && model.model_provider === agent.model.provider
        && model.api_base === agent.model.api_base,
    );
    if (previousModelIndex < 0 || previousModelIndex >= nextModels.length) {
      return agent;
    }

    const previousModel = previousModels[previousModelIndex];
    const nextModel = nextModels[previousModelIndex];
    const modelChanged = nextModel.model_name !== previousModel.model_name
      || nextModel.model_provider !== previousModel.model_provider
      || nextModel.api_base !== previousModel.api_base
      || nextModel.api_key !== previousModel.api_key;
    if (!modelChanged) return agent;

    changed = true;
    return {
      ...agent,
      model: {
        provider: nextModel.model_provider || "",
        api_base: nextModel.api_base || "",
        api_key: nextModel.api_key || "",
        model: nextModel.model_name || "",
      },
    };
  });

  return changed ? nextAgents : agents;
}

export function shouldContinueOpenAIAccountLoginPoll(error: unknown): boolean {
  return error instanceof Error
    && (error as Error & { retriable?: boolean }).retriable === true;
}

export function canAutoSaveOpenAIAccountModel(
  draftModels: ModelEntry[],
  persistedModels: ModelEntry[],
  identity: ModelIdentity,
): boolean {
  if (identity.originIndex === undefined || draftModels.length !== persistedModels.length) {
    return false;
  }

  const targetIndex = findModelIndex(draftModels, identity);
  const persistedTargetIndex = findModelIndex(persistedModels, identity);
  if (targetIndex < 0 || targetIndex !== persistedTargetIndex) return false;

  return draftModels.every((draftModel, index) => {
    const persistedModel = persistedModels[index];
    if (index === targetIndex) {
      return Boolean(persistedModel && modelNonAuthFieldsEqual(draftModel, persistedModel));
    }
    return Boolean(
      persistedModel
      && draftModel.origin_index === persistedModel.origin_index
      && modelEntriesEqual(draftModel, persistedModel),
    );
  });
}
