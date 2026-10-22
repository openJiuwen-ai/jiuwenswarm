export const DEFAULT_MOA_JEV_API_BASE = 'https://api.typesafe.ai';
export const DEFAULT_MOA_JEV_MODEL_NAME = 'jev-latest';

export type CollaborationJevDraft = {
  apiBase: string;
  modelName: string;
  apiKey: string;
};

export type CollaborationJevErrors = Partial<Record<keyof CollaborationJevDraft, string>>;

export function createCollaborationJevDraft(config: Readonly<Record<string, unknown>>): CollaborationJevDraft {
  return {
    apiBase: typeof config.moa_jev_api_base === 'string' ? config.moa_jev_api_base : DEFAULT_MOA_JEV_API_BASE,
    modelName:
      (typeof config.moa_jev_model_name === 'string' ? config.moa_jev_model_name.trim() : '') ||
      DEFAULT_MOA_JEV_MODEL_NAME,
    // Even an old or malformed response containing a key must never hydrate it.
    apiKey: '',
  };
}

export function parseCollaborationVerifierInterval(value: unknown): number | undefined {
  const raw = String(value ?? '').trim();
  if (!/^\d+$/.test(raw)) return undefined;
  const interval = Number(raw);
  return Number.isSafeInteger(interval) && interval > 0 ? interval : undefined;
}

export function validateCollaborationJevDraft(
  draft: CollaborationJevDraft,
  keyConfigured: boolean,
): CollaborationJevErrors {
  const errors: CollaborationJevErrors = {};
  try {
    const url = new URL(draft.apiBase.trim());
    if (!['http:', 'https:'].includes(url.protocol) || !url.hostname || url.username || url.password) {
      errors.apiBase = 'settingsPanel.multiModelCollaboration.errors.apiBaseInvalid';
    }
  } catch {
    errors.apiBase = 'settingsPanel.multiModelCollaboration.errors.apiBaseInvalid';
  }
  if (!draft.apiKey.trim() && !keyConfigured) errors.apiKey = 'settingsPanel.validation.required';
  return errors;
}

export function buildCollaborationJevUpdates(
  draft: CollaborationJevDraft,
  enableAfterSave: boolean,
): Record<string, string> {
  return {
    moa_jev_api_base: draft.apiBase.trim(),
    moa_jev_model_name: draft.modelName.trim() || DEFAULT_MOA_JEV_MODEL_NAME,
    ...(draft.apiKey.trim() ? { moa_jev_api_key: draft.apiKey.trim() } : {}),
    ...(enableAfterSave ? { moa_jev_enabled: 'true' } : {}),
  };
}
