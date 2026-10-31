// Generation-slot readiness for Design, shared by the landing gate and the
// canvas node actions. Kept in one place so a node refused by "regenerate"
// reports exactly the wording the landing page uses.

import i18n from '../../i18n';
import {
  isGenerationSlotConfigured,
  type GenerationSlot,
} from '../settings/modules/agent/generationModels';

export type GenerationKind = 'image' | 'video';

const SLOT: Record<GenerationKind, GenerationSlot> = {
  image: 'visual_gen',
  video: 'video_gen',
};

function asConfigBoolean(value: unknown): boolean {
  return value === true || String(value ?? '').trim().toLowerCase() === 'true';
}

/**
 * Why Design cannot generate this media kind, or null when the slot is switched
 * on and configured. Reads the same `config.get` fields the Settings page shows.
 */
export function generationProblemFromConfig(
  config: Readonly<Record<string, unknown>>,
  kind: GenerationKind,
): string | null {
  const slot = SLOT[kind];
  if (!asConfigBoolean(config[`${slot}_enabled`])) {
    return i18n.t(`designer.models.${kind}Off`);
  }
  if (!isGenerationSlotConfigured(config, slot)) {
    return i18n.t(`designer.models.${kind}Missing`);
  }
  return null;
}
