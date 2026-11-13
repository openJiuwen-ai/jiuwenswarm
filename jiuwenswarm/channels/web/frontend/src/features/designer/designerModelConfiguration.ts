import type { MultimodalSnapshot, PublicMultimodalModel } from '../settings/services/multimodalSettings';
import { isGenerationSlotConfigured, type GenerationSlot } from '../settings/modules/agent/generationModels';

export const DESIGN_GENERATION_SLOTS = ['video_gen', 'visual_gen'] as const;
export type DesignGenerationModel = {
  slot: GenerationSlot;
  model?: PublicMultimodalModel;
  status: 'missing' | 'incomplete' | 'disabled' | 'ready';
};

/** Derive availability from the same binding and field rules used by Settings. */
export function designGenerationModels(snapshot: MultimodalSnapshot): DesignGenerationModel[] {
  return DESIGN_GENERATION_SLOTS.map((slot) => {
    const binding = snapshot.tools[slot];
    const model = snapshot.models.find((item) => item.model_id === binding.model_id);
    const configured = model && isGenerationSlotConfigured({
      [`${slot}_provider`]: model.provider,
      [`${slot}_protocol`]: model.protocol,
      [`${slot}_api_base`]: model.api_base,
      [`${slot}_api_key`]: model.api_key_configured ? 'configured' : '',
      [`${slot}_model`]: model.model_name,
    }, slot);
    return { slot, model, status: !model ? 'missing' : !configured ? 'incomplete' : binding.enabled ? 'ready' : 'disabled' };
  });
}
