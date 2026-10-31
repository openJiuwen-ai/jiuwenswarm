// The design leader runs on the chat model, so without a usable one nothing on
// the canvas can run: the landing gate refuses the first prompt and the canvas
// chat dock warns instead of failing mid-round. Kept pure so both surfaces and
// the tests agree on what "ready" means.
//
// `models.list` answers with a single all-empty placeholder entry when nothing
// is configured (empty model_name, is_default false), so a non-empty catalog is
// not enough: at least one entry must carry a real model name (#5266).

import type { ModelEntry } from '../../types';

export function designChatModelReady(models: ModelEntry[] | null | undefined): boolean {
  if (!Array.isArray(models)) return false;
  return models.some((model) => String(model?.model_name ?? '').trim() !== '');
}
