import type { ModelPlan, VendorPresetMap } from '../../../../types';
import { normalizeContextWindowTokens, resolveDraftContextWindowTokens } from '../models/contextWindow';
import {
  CUSTOM_VENDOR_SELECTION,
  findVendorPreset,
  vendorSelectionKey,
  type ModelInputMode,
} from '../models/modelAdapters';
import {
  mediaCapabilityContextWindowField,
  mediaCapabilityEnabledField,
  type MediaCapabilityModality,
} from './mediaCapabilities';

const MODEL_PLANS: readonly ModelPlan[] = ['token_plan', 'coding_plan', 'custom_api'];

export type MediaModelDraft = {
  vendor_selection: string;
  protocol: 'openai';
  api_base: string;
  api_key: string;
  model_name: string;
  model_input_mode: ModelInputMode;
  provider: string;
  endpoint_profile: string;
  vendor_key: string;
  plan: string;
  context_window_tokens: string;
};

function isModelPlan(value: string): value is ModelPlan {
  return (MODEL_PLANS as readonly string[]).includes(value);
}

function readConfig(config: Readonly<Record<string, unknown>>, modality: MediaCapabilityModality, suffix: string) {
  return String(config[`${modality}_${suffix}`] ?? '');
}

export function createMediaModelDraft(
  config: Readonly<Record<string, unknown>>,
  modality: MediaCapabilityModality,
): MediaModelDraft {
  const apiBase = readConfig(config, modality, 'api_base');
  const apiKey = readConfig(config, modality, 'api_key');
  const modelName = readConfig(config, modality, 'model');
  const provider = readConfig(config, modality, 'provider');
  const endpointProfile = readConfig(config, modality, 'endpoint_profile');
  const vendorKey = readConfig(config, modality, 'vendor_key');
  const plan = readConfig(config, modality, 'plan');
  const contextWindowTokens = readConfig(config, modality, 'context_window_tokens');
  const normalizedContextWindowTokens = normalizeContextWindowTokens(contextWindowTokens);
  const hasProviderIdentity = Boolean(vendorKey.trim() && isModelPlan(plan.trim()));
  const hasLegacyConfig = [apiBase, apiKey, modelName, provider].some((value) => value.trim());

  return {
    vendor_selection: hasProviderIdentity
      ? vendorSelectionKey(plan.trim() as ModelPlan, vendorKey.trim())
      : hasLegacyConfig
        ? CUSTOM_VENDOR_SELECTION
        : '',
    protocol: 'openai',
    api_base: apiBase,
    api_key: apiKey,
    model_name: modelName,
    model_input_mode: hasProviderIdentity ? 'options' : 'manual',
    provider,
    endpoint_profile: endpointProfile,
    vendor_key: vendorKey,
    plan,
    context_window_tokens: normalizedContextWindowTokens,
  };
}

export function buildMediaModelConfigUpdates(
  draft: MediaModelDraft,
  catalog: VendorPresetMap,
  modality: MediaCapabilityModality,
  enableOnSave: boolean,
): Record<string, string> {
  const preset = findVendorPreset(catalog, draft.vendor_selection);
  return {
    [`${modality}_api_base`]: (preset?.api_base ?? draft.api_base).trim(),
    [`${modality}_api_key`]: draft.api_key.trim(),
    [`${modality}_model`]: draft.model_name.trim(),
    [`${modality}_provider`]: ((preset?.client_provider ?? draft.provider.trim()) || 'OpenAI').trim(),
    [`${modality}_endpoint_profile`]: (preset ? (preset.endpoint_profile ?? '') : draft.endpoint_profile).trim(),
    [`${modality}_vendor_key`]: (preset?.vendor_key ?? '').trim(),
    [`${modality}_plan`]: (preset?.plan ?? '').trim(),
    [mediaCapabilityContextWindowField(modality)]: String(resolveDraftContextWindowTokens(draft.context_window_tokens)),
    ...(enableOnSave ? { [mediaCapabilityEnabledField(modality)]: 'true' } : {}),
  };
}
