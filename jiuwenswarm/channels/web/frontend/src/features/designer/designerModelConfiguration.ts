import type { PublicMultimodalModel } from '../settings/services/multimodalSettings';

export const DESIGN_GENERATION_SLOTS = ['video_gen', 'visual_gen'] as const;
export type DesignGenerationSlot = (typeof DESIGN_GENERATION_SLOTS)[number];

export type DesignGenerationSelection = Record<DesignGenerationSlot, string | null>;

// Protocols whose native payload Design can call, including reference images.
// Keep this aligned with generation_selection.DESIGN_GENERATION_PROTOCOLS.
const DESIGN_GENERATION_PROTOCOLS = new Set(['minimax', 'modelark', 'dashscope', 'vllm-omni']);

export function designGenerationProtocolSupported(protocol: string | null | undefined): boolean {
  return DESIGN_GENERATION_PROTOCOLS.has(String(protocol ?? '').trim().toLowerCase());
}

/** Every multimodal model that can generate this modality, in catalogue order. */
export function modelsForSlot(
  models: readonly PublicMultimodalModel[],
  slot: DesignGenerationSlot,
): PublicMultimodalModel[] {
  return models.filter((model) => model.capabilities.includes(slot));
}

/** Catalogue rows of this modality whose protocol Design can call. */
export function supportedModelsForSlot(
  models: readonly PublicMultimodalModel[],
  slot: DesignGenerationSlot,
): PublicMultimodalModel[] {
  return modelsForSlot(models, slot).filter((model) => designGenerationProtocolSupported(model.protocol));
}

/**
 * Saved choice when that model is still selectable, otherwise the first
 * supported row. Unsupported rows stay visible but are not a choice. No
 * supported row resolves to null, the same as an empty modality.
 */
export function resolvedGenerationModelId(
  models: readonly PublicMultimodalModel[],
  slot: DesignGenerationSlot,
  savedId: string | null | undefined,
): string | null {
  const listed = supportedModelsForSlot(models, slot);
  const wanted = String(savedId ?? '').trim();
  if (wanted && listed.some((model) => model.model_id === wanted)) return wanted;
  return listed[0]?.model_id ?? null;
}

export function readGenerationSelection(
  value: unknown,
  models: readonly PublicMultimodalModel[],
): DesignGenerationSelection {
  const record = value && typeof value === 'object' ? value as Record<string, unknown> : {};
  const saved = (slot: DesignGenerationSlot) => (
    typeof record[slot] === 'string' ? record[slot] : null
  );
  return {
    video_gen: resolvedGenerationModelId(models, 'video_gen', saved('video_gen')),
    visual_gen: resolvedGenerationModelId(models, 'visual_gen', saved('visual_gen')),
  };
}

export function selectionAfterPick(
  current: DesignGenerationSelection,
  slot: DesignGenerationSlot,
  modelId: string,
  models: readonly PublicMultimodalModel[],
): DesignGenerationSelection {
  return readGenerationSelection({ ...current, [slot]: modelId }, models);
}

/** Slots that a reload started at ``reloadStartedEpoch`` must not overwrite.

A write may finish (and drop its pending mark) before a previously issued
``config.get`` resolves; any pick made after that reload started still has
to keep the current UI value.
*/
export function slotsToKeepOnReload(
  pendingSlots: Iterable<DesignGenerationSlot>,
  lastPickEpoch: ReadonlyMap<DesignGenerationSlot, number>,
  reloadStartedEpoch: number,
): DesignGenerationSlot[] {
  const pending = new Set(pendingSlots);
  return DESIGN_GENERATION_SLOTS.filter(
    (slot) => pending.has(slot) || (lastPickEpoch.get(slot) ?? 0) > reloadStartedEpoch,
  );
}

/** Keep in-flight picks when a later config reload would otherwise overwrite them. */
export function selectionForReload(
  saved: DesignGenerationSelection,
  current: DesignGenerationSelection,
  pendingSlots: Iterable<DesignGenerationSlot>,
  models: readonly PublicMultimodalModel[],
): DesignGenerationSelection {
  const pending = new Set(pendingSlots);
  return readGenerationSelection({
    video_gen: pending.has('video_gen') ? current.video_gen : saved.video_gen,
    visual_gen: pending.has('visual_gen') ? current.visual_gen : saved.visual_gen,
  }, models);
}
