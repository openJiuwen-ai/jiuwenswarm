export const JEV_PRESETS = {
  openrouter: {
    label: 'OpenRouter',
    api_base: 'https://openrouter.ai/api/alpha',
    model: 'typesafe/jev-1.13',
    keyField: 'jev_openrouter_api_key',
  },
  typesafe: {
    label: 'TypeSafe',
    api_base: 'https://api.typesafe.ai/v1',
    model: 'jev-1.13.0',
    keyField: 'jev_typesafe_api_key',
  },
  custom: { label: '', api_base: '', model: 'jev-1.13.0', keyField: 'jev_custom_api_key' },
} as const;

export type JevProvider = keyof typeof JEV_PRESETS;
export type JevDraft = {
  provider: JevProvider | '';
  api_base: string;
  api_key: string;
  model: string;
  display_name: string;
};

export function createEmptyJevDraft(): JevDraft {
  return { provider: '', api_base: '', api_key: '', model: '', display_name: '' };
}

export function createJevDraft(
  values: Record<string, unknown>, config: Record<string, unknown>,
): JevDraft & { provider: JevProvider } {
  const provider = Object.hasOwn(JEV_PRESETS, String(values.decision_provider))
    ? values.decision_provider as JevProvider
    : 'typesafe';
  const preset = JEV_PRESETS[provider];
  return {
    provider,
    api_base: String(values.decision_api_base || preset.api_base),
    model: String(values.decision_model || preset.model),
    display_name: String(values.decision_display_name || values.decision_model || preset.model),
    api_key: String(config[preset.keyField] ?? ''),
  };
}

export function validateJevDraft(draft: JevDraft): Partial<Record<keyof JevDraft, string>> {
  if (!draft.provider) return { provider: 'settingsPanel.models.validation.vendorSelectionRequired' };
  const errors: Partial<Record<keyof JevDraft, string>> = {};
  if (!draft.api_key.trim()) errors.api_key = 'settingsPanel.validation.required';
  if (!draft.model.trim()) errors.model = 'settingsPanel.validation.required';
  if (!draft.display_name.trim()) errors.display_name = 'settingsPanel.models.validation.aliasRequired';
  else if (draft.display_name.trim().length > 100) errors.display_name = 'config.modelList.aliasTooLong';
  const openRouterModel = /^(?:typesafe\/jev-\d+\.\d+(?:\.\d+)?(?:-\d{8})?|~typesafe\/jev-latest)$/;
  if (draft.provider === 'openrouter' && !openRouterModel.test(draft.model.trim())) {
    errors.model = 'settingsPanel.browser.jevModelInvalid';
  }
  try {
    const url = new URL(draft.api_base.trim());
    if (url.protocol !== 'https:' || url.username || url.password || url.search || url.hash) {
      errors.api_base = 'settingsPanel.browser.jevBaseInvalid';
    }
  } catch {
    errors.api_base = 'settingsPanel.browser.jevBaseInvalid';
  }
  return errors;
}
