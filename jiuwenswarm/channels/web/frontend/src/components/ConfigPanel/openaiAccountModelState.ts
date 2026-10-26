import type { ModelEntry } from "../../types";

export interface ModelIdentity {
  originIndex?: number;
  fallbackIndex: number;
}

interface AgentModelBinding {
  ref?: string;
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

export function resolveConfiguredModelIndex(models: ModelEntry[], modelRef: string): number {
  const separatorIndex = modelRef.lastIndexOf('#');
  if (separatorIndex >= 0) {
    const modelName = modelRef.slice(0, separatorIndex);
    const modelIndex = Number(modelRef.slice(separatorIndex + 1));
    return Number.isInteger(modelIndex) && modelIndex >= 0 && modelIndex < models.length && models[modelIndex].model_name === modelName ? modelIndex : -1;
  }

  const matchingIndexes = models.map((model, index) => (model.model_name === modelRef ? index : -1)).filter(index => index >= 0);
  return matchingIndexes.length === 1 ? matchingIndexes[0] : -1;
}

export function syncAgentsWithModelChanges<T extends AgentWithModelBinding>(
  agents: T[],
  previousModels: ModelEntry[],
  nextModels: ModelEntry[],
): T[] {
  let changed = false;
  const nextAgents = agents.map((agent) => {
    const refSeparator = agent.model.ref?.lastIndexOf("#") ?? -1;
    const refIndex = refSeparator >= 0 ? Number(agent.model.ref?.slice(refSeparator + 1)) : -1;
    const referencedModel = Number.isInteger(refIndex) ? previousModels[refIndex] : undefined;
    const referencedModelMatches = referencedModel?.model_name === agent.model.model
      && referencedModel.model_provider === agent.model.provider
      && referencedModel.api_base === agent.model.api_base;
    const previousModelIndex = referencedModelMatches
      ? refIndex
      : previousModels.findIndex(
        (model) => model.model_name === agent.model.model
        && model.model_provider === agent.model.provider
        && model.api_base === agent.model.api_base,
      );
    if (previousModelIndex < 0) {
      return agent;
    }

    const previousModel = previousModels[previousModelIndex];
    const unchangedObjectIndex = nextModels.indexOf(previousModel);
    let nextModelIndex = previousModel.origin_index === undefined
      ? unchangedObjectIndex
      : nextModels.findIndex((model) => model.origin_index === previousModel.origin_index);
    if (nextModelIndex < 0 && previousModel.origin_index === undefined && previousModelIndex < nextModels.length) {
      nextModelIndex = previousModelIndex;
    }
    if (nextModelIndex < 0) return agent;

    const nextModel = nextModels[nextModelIndex];
    const modelChanged = nextModel.model_name !== previousModel.model_name
      || nextModel.model_provider !== previousModel.model_provider
      || nextModel.api_base !== previousModel.api_base
      || nextModel.api_key !== previousModel.api_key
      || nextModelIndex !== previousModelIndex;
    if (!modelChanged) return agent;

    changed = true;
    return {
      ...agent,
      model: {
        ref: `${nextModel.model_name || ""}#${nextModelIndex}`,
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
