// Generation-catalogue readiness for Design. Landing, the canvas chat send
// and a single-node regenerate all use the same rule: each modality needs at
// least one model Design can call. Rows with any other protocol stay listed
// but do not count. The agent tool switch and its bound model are not part
// of this check.

import i18n from '../../i18n';
import type { PublicMultimodalModel } from '../settings/services/multimodalSettings';
import { supportedModelsForSlot, type DesignGenerationSlot } from './designerModelConfiguration';

export type GenerationKind = 'image' | 'video';

const SLOT: Record<GenerationKind, DesignGenerationSlot> = {
  image: 'visual_gen',
  video: 'video_gen',
};

/** Why Design cannot generate this media kind, or null when a supported model exists. */
export function generationCatalogProblem(
  models: readonly PublicMultimodalModel[],
  kind: GenerationKind,
): string | null {
  if (supportedModelsForSlot(models, SLOT[kind]).length > 0) return null;
  return i18n.t(`designer.models.${kind}Missing`);
}
