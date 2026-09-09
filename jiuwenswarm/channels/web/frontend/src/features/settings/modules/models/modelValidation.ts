import type { ModelEntry, VendorPresetMap } from '../../../../types';
import { CUSTOM_VENDOR_SELECTION, OPENAI_ACCOUNT_SELECTION, findVendorPreset, type ModelDraft } from './modelAdapters';
import { isReasoningLevelSupported, resolveModelReasoning } from './modelReasoning';

export type ModelDraftErrors = Partial<Record<keyof ModelDraft, string>>;

const MAX_API_KEY_LENGTH = 2048;

export function validateModelDraft(
  value: ModelDraft,
  models: ModelEntry[],
  editingOriginIndex: number | undefined,
  catalog: VendorPresetMap,
  t: (key: string, values?: Record<string, unknown>) => string,
): ModelDraftErrors {
  const errors: ModelDraftErrors = {};
  const alias = value.alias.trim();
  const modelName = value.model_name.trim();
  const apiBase = value.api_base.trim();
  const apiKey = value.api_key.trim();
  const account = value.vendor_selection === OPENAI_ACCOUNT_SELECTION;
  const preset = findVendorPreset(catalog, value.vendor_selection);

  if (alias.length > 100) errors.alias = t('config.modelList.aliasTooLong');

  if (!value.vendor_selection) {
    errors.vendor_selection = t('settingsPanel.models.validation.vendorSelectionRequired');
  } else if (
    value.vendor_selection !== CUSTOM_VENDOR_SELECTION &&
    value.vendor_selection !== OPENAI_ACCOUNT_SELECTION &&
    !findVendorPreset(catalog, value.vendor_selection)
  ) {
    errors.vendor_selection = t('settingsPanel.models.validation.vendorSelectionInvalid');
  }

  if (!modelName) errors.model_name = t('config.modelList.modelNameRequired');
  else if (modelName.length > 100) errors.model_name = t('config.modelList.modelNameTooLong');

  if (value.protocol === 'anthropic' && preset && (!preset.supports_anthropic || !preset.anthropic_base)) {
    errors.protocol = t('settingsPanel.models.validation.anthropicUnavailable');
  }

  if (!account) {
    if (!apiBase) errors.api_base = t('config.modelList.apiBaseRequired');
    else if (apiBase.length > 512) errors.api_base = t('config.modelList.apiBaseTooLong');
    else if (!/^https?:\/\//i.test(apiBase)) errors.api_base = t('config.modelList.apiBaseUrlInvalid');
    if (apiKey.length > MAX_API_KEY_LENGTH) errors.api_key = t('settingsPanel.models.apiKeyTooLong');
    else if (!apiKey) errors.api_key = t('config.modelList.apiKeyRequired');
  }

  const reasoning = resolveModelReasoning(catalog, preset, modelName, value.protocol);
  if (!catalog.reasoning) {
    errors.reasoning_level = t('settingsPanel.models.validation.reasoningUnavailable');
  } else if (reasoning && !isReasoningLevelSupported(value.reasoning_level, reasoning)) {
    errors.reasoning_level = t('settingsPanel.models.validation.reasoningUnsupported', {
      value: value.reasoning_level,
    });
  }

  const others = models.filter(
    (model) =>
      model.is_free !== true &&
      model.is_agentos !== true &&
      (editingOriginIndex === undefined || model.origin_index !== editingOriginIndex),
  );
  if (
    alias &&
    !errors.alias &&
    others.some((model) => model.alias?.trim() === alias || model.model_name.trim() === alias)
  ) {
    errors.alias = t('settingsPanel.models.validation.aliasConflict');
  }
  if (!errors.model_name && others.some((model) => model.alias?.trim() === modelName)) {
    errors.model_name = t('settingsPanel.models.validation.modelNameConflict');
  }

  return errors;
}
