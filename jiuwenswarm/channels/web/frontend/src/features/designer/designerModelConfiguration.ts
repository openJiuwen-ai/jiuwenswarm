import type { PublicMultimodalModel } from '../settings/services/multimodalSettings';

export const DESIGN_GENERATION_SLOTS = ['video_gen', 'visual_gen'] as const;
export type DesignGenerationSlot = (typeof DESIGN_GENERATION_SLOTS)[number];

export type DesignGenerationSelection = Record<DesignGenerationSlot, string | null>;

/** Every multimodal model that can generate this modality, in catalogue order. */
export function modelsForSlot(
  models: readonly PublicMultimodalModel[],
  slot: DesignGenerationSlot,
): PublicMultimodalModel[] {
  return models.filter((model) => model.capabilities.includes(slot));
}

/**
 * Saved choice when that model is still in the modality, otherwise the first.
 * An empty catalogue resolves to null.
 */
export function resolvedGenerationModelId(
  models: readonly PublicMultimodalModel[],
  slot: DesignGenerationSlot,
  savedId: string | null | undefined,
): string | null {
  const listed = modelsForSlot(models, slot);
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
