import type { ModelEntry } from '../../../../types';

export const MAX_MODEL_CONFIG_IMPORT_BYTES = 1024 * 1024;
const MAX_MODELS = 32;

type JsonObject = Record<string, unknown>;

function isObject(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function number(value: unknown): number | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : undefined;
  }
  return undefined;
}

function entries(value: unknown): unknown[] {
  if (Array.isArray(value)) return value;
  if (!isObject(value)) return [];
  if (Array.isArray(value.models)) return value.models;
  if (isObject(value.models) && Array.isArray(value.models.defaults)) return value.models.defaults;
  if (Array.isArray(value.defaults)) return value.defaults;
  return [value];
}

function normalizeEntry(value: unknown, index: number): ModelEntry {
  if (!isObject(value)) throw new Error(`models[${index}] must be an object`);
  const client = isObject(value.model_client_config) ? value.model_client_config : value;
  const request = isObject(value.model_config_obj) ? value.model_config_obj : value;
  const modelName =
    text(client.model_name) ||
    text(client.model) ||
    text(value.model_name) ||
    text(value.model) ||
    text(value.MODEL_NAME);
  const apiBase = text(client.api_base) || text(value.api_base) || text(value.API_BASE);
  const apiKey = text(client.api_key) || text(value.api_key) || text(value.API_KEY);
  const provider =
    text(client.client_provider) ||
    text(client.model_provider) ||
    text(value.model_provider) ||
    text(value.provider) ||
    text(value.MODEL_PROVIDER);
  if (!modelName || !apiBase || !provider) {
    throw new Error(`models[${index}] requires model_name, api_base, and model_provider`);
  }
  const result: ModelEntry = {
    model_name: modelName,
    api_base: apiBase,
    api_key: apiKey,
    model_provider: provider,
    is_default: typeof value.is_default === 'boolean' ? value.is_default : index === 0,
  };
  const alias = text(value.alias);
  const reasoningLevel = text(request.reasoning_level) || text(value.reasoning_level);
  const timeout = number(client.timeout) ?? number(value.timeout);
  const temperature = number(request.temperature) ?? number(value.temperature);
  if (alias) result.alias = alias;
  if (reasoningLevel) result.reasoning_level = reasoningLevel;
  if (timeout !== undefined) result.timeout = timeout;
  if (temperature !== undefined) result.temperature = temperature;
  return result;
}

/** Parse either the WebUI export shape or Python-compatible models.defaults JSON. */
export function parseModelConfigurationJson(source: string): ModelEntry[] {
  if (new TextEncoder().encode(source).byteLength > MAX_MODEL_CONFIG_IMPORT_BYTES) {
    throw new Error('Model configuration file exceeds 1 MiB');
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(source);
  } catch {
    throw new Error('Model configuration must be valid JSON');
  }
  const rawEntries = entries(parsed);
  if (!rawEntries.length) throw new Error('Model configuration does not contain any models');
  if (rawEntries.length > MAX_MODELS) throw new Error(`Model configuration exceeds ${MAX_MODELS} models`);
  const models = rawEntries.map(normalizeEntry);
  if (!models.some((model) => model.is_default)) models[0].is_default = true;
  return models;
}
