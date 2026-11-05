// The design leader runs on the chat model, so without a usable one nothing on
// the canvas can run: the landing gate refuses the first prompt and the canvas
// chat dock warns instead of failing mid-round. Kept pure so both surfaces and
// the tests agree on what "ready" means.
//
// `models.list` answers with a single all-empty placeholder entry when nothing
// is configured (empty model_name, is_default false), so a non-empty catalog is
// not enough: at least one entry must carry a real model name (#5266).

import { resolveChatModelSelection } from '../../stores/sessionStore';
import type { ModelEntry } from '../../types';

export function designChatModelReady(models: ModelEntry[] | null | undefined): boolean {
  if (!Array.isArray(models)) return false;
  return models.some((model) => String(model?.model_name ?? '').trim() !== '');
}

/**
 * True when the chat model Design will send is configured with the Anthropic
 * protocol (`model_provider === 'Anthropic'`, the settings control-panel value).
 * Other models in the catalog do not count.
 */
export function designChatModelUsesAnthropic(
  models: ModelEntry[] | null | undefined,
  selectedModelName: string | null | undefined,
  defaultModelName: string | null | undefined,
): boolean {
  if (!Array.isArray(models)) return false;
  const model = resolveChatModelSelection(models, selectedModelName ?? null, defaultModelName ?? null);
  return model?.model_provider === 'Anthropic';
}
