import type { ModelEntry, ModelPlan, VendorPreset, VendorPresetMap } from '../../../../types';
import {
  isReasoningLevelSupported,
  parseReasoningCapabilities,
  parseReasoningCatalog,
  parseReasoningRules,
  resolveModelReasoning,
} from './modelReasoning';

export type ModelProtocol = 'openai' | 'anthropic';
export type ModelInputMode = 'options' | 'manual';

export const CUSTOM_VENDOR_SELECTION = 'custom';
export const OPENAI_ACCOUNT_SELECTION = 'openai-account';
export const OPENAI_ACCOUNT_DEFAULT_API_BASE = 'https://chatgpt.com/backend-api/codex';

export type ModelDraft = {
  alias: string;
  protocol: ModelProtocol;
  vendor_selection: string;
  model_name: string;
  model_input_mode: ModelInputMode;
  api_key: string;
  api_base: string;
  reasoning_level: string;
  is_default: boolean;
};

const MODEL_DRAFT_FIELDS: readonly (keyof ModelDraft)[] = [
  'alias',
  'protocol',
  'vendor_selection',
  'model_name',
  'model_input_mode',
  'api_key',
  'api_base',
  'reasoning_level',
  'is_default',
];

export function rebaseModelDraft(
  current: ModelDraft,
  previousBaseline: ModelDraft,
  nextBaseline: ModelDraft,
): ModelDraft {
  return Object.fromEntries(
    MODEL_DRAFT_FIELDS.map((field) => [
      field,
      Object.is(current[field], previousBaseline[field]) ? nextBaseline[field] : current[field],
    ]),
  ) as ModelDraft;
}

export function vendorSelectionKey(plan: ModelPlan, vendorKey: string): string {
  return `${plan}:${vendorKey}`;
}

export function flattenVendorCatalog(catalog: VendorPresetMap): VendorPreset[] {
  return [...catalog.token_plan, ...catalog.coding_plan, ...catalog.custom_api];
}

function isNullableString(value: unknown): value is string | null | undefined {
  return value === undefined || value === null || typeof value === 'string';
}

export function parseVendorCatalog(value: unknown): VendorPresetMap {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('INVALID_VENDOR_CATALOG');
  const payload = value as Record<string, unknown>;
  const result = {} as VendorPresetMap;
  try {
    result.reasoning = parseReasoningCatalog(payload.reasoning);
  } catch {
    throw new Error('INVALID_VENDOR_CATALOG');
  }
  for (const plan of ['token_plan', 'coding_plan', 'custom_api'] as const) {
    const presets = payload[plan];
    if (!Array.isArray(presets)) throw new Error('INVALID_VENDOR_CATALOG');
    result[plan] = presets.map((entry) => {
      if (!entry || typeof entry !== 'object' || Array.isArray(entry)) throw new Error('INVALID_VENDOR_CATALOG');
      const preset = entry as Record<string, unknown>;
      if (
        typeof preset.vendor_key !== 'string' ||
        typeof preset.display_name !== 'string' ||
        preset.plan !== plan ||
        typeof preset.client_provider !== 'string' ||
        typeof preset.api_base !== 'string' ||
        !isNullableString(preset.endpoint_profile) ||
        typeof preset.default_model !== 'string' ||
        !Array.isArray(preset.model_options) ||
        !preset.model_options.every((model) => typeof model === 'string') ||
        typeof preset.icon_key !== 'string' ||
        !isNullableString(preset.models_endpoint) ||
        typeof preset.models_needs_key !== 'boolean' ||
        typeof preset.supports_anthropic !== 'boolean' ||
        !isNullableString(preset.anthropic_base) ||
        !isNullableString(preset.anthropic_client_provider)
      ) {
        throw new Error('INVALID_VENDOR_CATALOG');
      }
      try {
        return {
          ...preset,
          reasoning_capabilities: parseReasoningCapabilities(preset.reasoning_capabilities),
          reasoning_rules: parseReasoningRules(preset.reasoning_rules),
        } as unknown as VendorPreset;
      } catch {
        throw new Error('INVALID_VENDOR_CATALOG');
      }
    });
  }
  return result;
}

export function findVendorPreset(catalog: VendorPresetMap, selection: string): VendorPreset | undefined {
  return flattenVendorCatalog(catalog).find(
    (preset) => vendorSelectionKey(preset.plan, preset.vendor_key) === selection,
  );
}

/** Normalize the draft after capabilities load or the model changes; never persist it here. */
export function reconcileModelReasoning(draft: ModelDraft, catalog: VendorPresetMap): ModelDraft {
  const capability = resolveModelReasoning(
    catalog,
    findVendorPreset(catalog, draft.vendor_selection),
    draft.model_name,
    draft.protocol,
  );
  return capability && !isReasoningLevelSupported(draft.reasoning_level, capability)
    ? { ...draft, reasoning_level: '' }
    : draft;
}

export function normalizeModelOptions(options: readonly string[]): string[] {
  return Array.from(new Set(options.map((option) => option.trim()).filter(Boolean)));
}

export function selectProviderDefaultModel(currentModel: string, options: readonly string[]): string {
  const normalizedOptions = normalizeModelOptions(options);
  const normalizedCurrentModel = currentModel.trim();
  return normalizedOptions.includes(normalizedCurrentModel) ? normalizedCurrentModel : (normalizedOptions[0] ?? '');
}

export function resolveModelPreset(model: ModelEntry, catalog: VendorPresetMap): VendorPreset | undefined {
  if (!model.vendor_key) return undefined;
  if (model.plan) {
    return findVendorPreset(catalog, vendorSelectionKey(model.plan, model.vendor_key));
  }
  const matches = flattenVendorCatalog(catalog).filter((preset) => preset.vendor_key === model.vendor_key);
  return matches.length === 1 ? matches[0] : undefined;
}

export function createModelDraft(model: ModelEntry | undefined, catalog: VendorPresetMap): ModelDraft {
  if (!model) {
    return {
      alias: '',
      protocol: 'openai',
      vendor_selection: '',
      model_name: '',
      model_input_mode: 'options',
      api_key: '',
      api_base: '',
      reasoning_level: '',
      is_default: false,
    };
  }

  const alias = model.alias ?? '';

  if (model.model_provider === 'OpenAIAccount') {
    return {
      alias,
      protocol: 'openai',
      vendor_selection: OPENAI_ACCOUNT_SELECTION,
      model_name: model.model_name,
      model_input_mode: 'options',
      api_key: '',
      api_base: model.api_base,
      reasoning_level: model.reasoning_level ?? '',
      is_default: model.is_default ?? false,
    };
  }

  const preset = resolveModelPreset(model, catalog);
  const protocol: ModelProtocol = model.model_provider === 'Anthropic' ? 'anthropic' : 'openai';
  const vendorSelection = preset
    ? vendorSelectionKey(preset.plan, preset.vendor_key)
    : model.vendor_key
      ? ''
      : CUSTOM_VENDOR_SELECTION;
  return {
    alias,
    protocol,
    vendor_selection: vendorSelection,
    model_name: model.model_name,
    model_input_mode: preset ? 'options' : 'manual',
    api_key: model.api_key,
    api_base: model.api_base,
    reasoning_level: model.reasoning_level ?? '',
    is_default: model.is_default ?? false,
  };
}

export function applyVendorSelection(draft: ModelDraft, selection: string, catalog: VendorPresetMap): ModelDraft {
  if (selection === OPENAI_ACCOUNT_SELECTION) {
    return {
      ...draft,
      protocol: 'openai',
      vendor_selection: selection,
      model_name: '',
      model_input_mode: 'options',
      api_key: '',
      api_base: OPENAI_ACCOUNT_DEFAULT_API_BASE,
    };
  }

  if (selection === CUSTOM_VENDOR_SELECTION) {
    return {
      ...draft,
      vendor_selection: selection,
      model_name: '',
      model_input_mode: 'manual',
      api_key: '',
      api_base: '',
    };
  }

  const preset = findVendorPreset(catalog, selection);
  if (!preset) return { ...draft, vendor_selection: selection };
  const anthropic = draft.protocol === 'anthropic' && preset.supports_anthropic;
  return {
    ...draft,
    protocol: anthropic ? 'anthropic' : 'openai',
    vendor_selection: selection,
    model_name: preset.default_model,
    model_input_mode: 'options',
    api_key: '',
    api_base: anthropic ? (preset.anthropic_base ?? '') : preset.api_base,
  };
}

export function applyModelProtocol(draft: ModelDraft, protocol: ModelProtocol, catalog: VendorPresetMap): ModelDraft {
  const preset = findVendorPreset(catalog, draft.vendor_selection);
  return {
    ...draft,
    protocol,
    api_base: preset ? (protocol === 'anthropic' ? (preset.anthropic_base ?? '') : preset.api_base) : draft.api_base,
  };
}

export function modelDraftToEntry(
  draft: ModelDraft,
  existing: ModelEntry | undefined,
  catalog: VendorPresetMap,
  connectionChanged: boolean,
): ModelEntry {
  const next: ModelEntry = {
    ...existing,
    alias: draft.alias.trim(),
    model_name: draft.model_name.trim(),
    api_key: draft.api_key.trim(),
    api_base: draft.api_base.trim(),
    model_provider: existing?.model_provider ?? '',
    reasoning_level: draft.reasoning_level,
    is_default: draft.is_default,
  };

  if (draft.vendor_selection === OPENAI_ACCOUNT_SELECTION) {
    next.model_provider = 'OpenAIAccount';
    next.api_key = '';
    delete next.vendor_key;
    delete next.plan;
    delete next.endpoint_profile;
    return next;
  }

  const preset = findVendorPreset(catalog, draft.vendor_selection);
  if (preset) {
    next.vendor_key = preset.vendor_key;
    next.plan = preset.plan;
    if (draft.protocol === 'anthropic') {
      next.model_provider = preset.anthropic_client_provider ?? '';
      next.api_base = preset.anthropic_base ?? '';
      delete next.endpoint_profile;
    } else {
      next.model_provider = preset.client_provider;
      next.api_base = preset.api_base;
      if (preset.endpoint_profile) next.endpoint_profile = preset.endpoint_profile;
      else delete next.endpoint_profile;
    }
    return next;
  }

  delete next.vendor_key;
  delete next.plan;
  if (!existing || connectionChanged) {
    next.model_provider = draft.protocol === 'anthropic' ? 'Anthropic' : 'OpenAI';
    delete next.endpoint_profile;
  }
  return next;
}

export function displayModelProtocol(model: ModelEntry): ModelProtocol {
  return model.model_provider === 'Anthropic' ? 'anthropic' : 'openai';
}
