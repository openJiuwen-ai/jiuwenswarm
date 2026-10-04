export type ArtifactAiEditSubmitMode = 'auto_send' | 'fill_only';

export const ARTIFACT_AI_EDIT_SUBMIT_MODE_KEY = 'jiuwenswarm_artifact_ai_edit_submit_mode';
const DEFAULT_MODE: ArtifactAiEditSubmitMode = 'auto_send';

function storage(): Storage | null {
  try {
    if (typeof globalThis !== 'undefined' && 'localStorage' in globalThis && globalThis.localStorage) {
      return globalThis.localStorage;
    }
  } catch {
    /* ignore */
  }
  return null;
}

export function readArtifactAiEditSubmitMode(): ArtifactAiEditSubmitMode {
  const store = storage();
  if (!store) return DEFAULT_MODE;
  try {
    const stored = store.getItem(ARTIFACT_AI_EDIT_SUBMIT_MODE_KEY);
    return stored === 'fill_only' ? 'fill_only' : DEFAULT_MODE;
  } catch {
    return DEFAULT_MODE;
  }
}

export function persistArtifactAiEditSubmitMode(mode: ArtifactAiEditSubmitMode): void {
  const store = storage();
  if (!store) return;
  try {
    store.setItem(ARTIFACT_AI_EDIT_SUBMIT_MODE_KEY, mode);
  } catch {
    /* ignore */
  }
}
