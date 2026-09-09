import type { ModelPlan, VendorPreset, VendorPresetMap } from '../../../../types';
import {
  CUSTOM_VENDOR_SELECTION,
  findVendorPreset,
  vendorSelectionKey,
  type ModelInputMode,
} from '../models/modelAdapters';
import { mediaCapabilityEnabledField, type MediaCapabilityModality } from './mediaCapabilities';

const MODEL_PLANS: readonly ModelPlan[] = ['token_plan', 'coding_plan', 'custom_api'];

const GENERATION_MODALITIES = new Set<MediaCapabilityModality>(['image_gen', 'video_gen']);

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
};

function isModelPlan(value: string): value is ModelPlan {
  return (MODEL_PLANS as readonly string[]).includes(value);
}

export function isGenerationModality(modality: MediaCapabilityModality): boolean {
  return GENERATION_MODALITIES.has(modality);
}

export function usesDedicatedVideoGenModels(preset: VendorPreset | undefined): boolean {
  return Boolean(preset?.video_gen_model_options?.length);
}

export function usesDedicatedImageGenModels(preset: VendorPreset | undefined): boolean {
  return Boolean(preset?.image_gen_model_options?.length);
}

export function usesDedicatedMediaModels(
  preset: VendorPreset | undefined,
  modality: MediaCapabilityModality,
): boolean {
  if (modality === 'video_gen') return usesDedicatedVideoGenModels(preset);
  if (modality === 'image_gen') return usesDedicatedImageGenModels(preset);
  return false;
}

export function mediaModelOptionsForPreset(
  preset: VendorPreset,
  modality: MediaCapabilityModality,
): readonly string[] {
  // Generation tools must never fall back to the chat /v1/models catalog.
  if (modality === 'video_gen') {
    return usesDedicatedVideoGenModels(preset) ? (preset.video_gen_model_options ?? []) : [];
  }
  if (modality === 'image_gen') {
    return usesDedicatedImageGenModels(preset) ? (preset.image_gen_model_options ?? []) : [];
  }
  return preset.model_options;
}

export function mediaDefaultModelForPreset(
  preset: VendorPreset,
  modality: MediaCapabilityModality,
): string {
  if (modality === 'video_gen' && preset.video_gen_default_model?.trim()) {
    return preset.video_gen_default_model.trim();
  }
  if (modality === 'image_gen' && preset.image_gen_default_model?.trim()) {
    return preset.image_gen_default_model.trim();
  }
  if (isGenerationModality(modality)) {
    const options = mediaModelOptionsForPreset(preset, modality);
    return options[0] ?? '';
  }
  return preset.default_model;
}

export function mediaApiBaseForPreset(
  preset: VendorPreset,
  modality: MediaCapabilityModality,
): string {
  if (modality === 'video_gen' && preset.video_gen_api_base?.trim()) {
    return preset.video_gen_api_base.trim();
  }
  if (modality === 'image_gen' && preset.image_gen_api_base?.trim()) {
    return preset.image_gen_api_base.trim();
  }
  return preset.api_base;
}

export function shouldFetchRemoteMediaModels(
  preset: VendorPreset | undefined,
  modality: MediaCapabilityModality,
): boolean {
  // Image/video generation IDs are not listed by chat models endpoints.
  if (isGenerationModality(modality)) return false;
  if (!preset?.models_endpoint) return false;
  if (usesDedicatedMediaModels(preset, modality)) return false;
  return true;
}

/** Keep only vendors that publish dedicated generation models (plus Custom). */
export function filterVendorCatalogForModality(
  catalog: VendorPresetMap,
  modality: MediaCapabilityModality,
): VendorPresetMap {
  if (!isGenerationModality(modality)) return catalog;
  const filterPlan = (presets: VendorPreset[]) =>
    presets.filter((preset) => usesDedicatedMediaModels(preset, modality));
  return {
    reasoning: catalog.reasoning,
    token_plan: filterPlan(catalog.token_plan),
    coding_plan: filterPlan(catalog.coding_plan),
    custom_api: filterPlan(catalog.custom_api),
  };
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
    [`${modality}_api_base`]: (preset ? mediaApiBaseForPreset(preset, modality) : draft.api_base).trim(),
    [`${modality}_api_key`]: draft.api_key.trim(),
    [`${modality}_model`]: draft.model_name.trim(),
    [`${modality}_provider`]: ((preset?.client_provider ?? draft.provider.trim()) || 'OpenAI').trim(),
    [`${modality}_endpoint_profile`]: (preset ? (preset.endpoint_profile ?? '') : draft.endpoint_profile).trim(),
    [`${modality}_vendor_key`]: (preset?.vendor_key ?? '').trim(),
    [`${modality}_plan`]: (preset?.plan ?? '').trim(),
    ...(enableOnSave ? { [mediaCapabilityEnabledField(modality)]: 'true' } : {}),
  };
}
