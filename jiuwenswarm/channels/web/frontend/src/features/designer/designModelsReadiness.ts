// Design landing gate (frontend-only): before the first prompt creates a
// session / graph, both generation slots (video / image) and the chat model
// must be configured and switched on in Settings. Reuses the Settings
// contract (`config.get` + generation-slot helpers) so the check reports
// exactly what the Settings page shows. Called from the design-mode entry
// points only — other modes are untouched.

import i18n from '../../i18n';
import { webRequest } from '../../services/webClient';
import { useSessionStore } from '../../stores/sessionStore';
import { isGenerationSlotConfigured } from '../settings/modules/agent/generationModels';
import type { ModelEntry } from '../../types';

function asConfigBoolean(value: unknown): boolean {
  return value === true || String(value ?? '').trim().toLowerCase() === 'true';
}

/**
 * User-facing problems blocking a design prompt, or null when everything the
 * design pipeline drives (chat / video / image) is configured and switched on.
 * Never throws: a failed config read must not block entry.
 */
export async function findDesignModelsProblem(): Promise<string | null> {
  let config: Record<string, unknown> = {};
  try {
    config = (await webRequest<Record<string, unknown>>('config.get')) ?? {};
  } catch {
    return null;
  }
  const problems: string[] = [];
  if (useSessionStore.getState().chatAvailableModels.length === 0) {
    // The startup models fetch can time out while the runtime is still warming
    // up — re-verify with a fresh `models.list` before claiming the chat model
    // is missing, and fail open when the re-check itself cannot complete.
    try {
      const resp = await webRequest<{ models?: ModelEntry[]; active_model?: string }>('models.list');
      const models = Array.isArray(resp?.models) ? resp.models : [];
      if (models.length > 0) {
        useSessionStore.getState().setAvailableModels(models, resp?.active_model ?? '');
      } else {
        problems.push(i18n.t('designer.models.chatMissing'));
      }
    } catch {
      // Cannot verify right now: do not claim the chat model is missing.
    }
  }
  if (!asConfigBoolean(config.video_gen_enabled)) {
    problems.push(i18n.t('designer.models.videoOff'));
  } else if (!isGenerationSlotConfigured(config, 'video_gen')) {
    problems.push(i18n.t('designer.models.videoMissing'));
  }
  if (!asConfigBoolean(config.visual_gen_enabled)) {
    problems.push(i18n.t('designer.models.imageOff'));
  } else if (!isGenerationSlotConfigured(config, 'visual_gen')) {
    problems.push(i18n.t('designer.models.imageMissing'));
  }
  return problems.length > 0 ? problems.join(' ') : null;
}
