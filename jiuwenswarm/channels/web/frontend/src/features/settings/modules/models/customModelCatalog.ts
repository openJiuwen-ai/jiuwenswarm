/**
 * Shared helpers for the model-catalogue status of the model settings dialogs.
 *
 * The default/dialog model editor and the multimodal slot editor both show a
 * model dropdown with a refresh action. Preset providers are fetched through
 * `vendors.fetch_models`; custom gateways are fetched through
 * `vendors.fetch_custom_models`, a Gateway-local RPC served by the
 * `custom_model_catalog` extension.
 *
 * The method name, the reason-to-copy mapping, the endpoint guard, the status
 * hints and the dedupe keys must stay identical across the two dialogs, so they
 * live here instead of being copied into each component.
 */

import type { VendorPreset } from '../../../../types';

/** Gateway-local RPC that fetches the catalogue of a user-configured endpoint. */
export const CUSTOM_FETCH_METHOD = 'vendors.fetch_custom_models';

/** Stable reason phrases from the extension, mapped to `settingsPanel.models.fetchReasons` keys. */
export const FETCH_REASON_KEYS: Record<string, string> = {
  'no remote models endpoint': 'noEndpoint',
  'api_base required for fetch': 'apiBaseRequired',
  'api_base must be an http(s) URL': 'apiBaseInvalid',
  'api_key required for fetch': 'apiKeyRequired',
  'api_key too long for fetch': 'apiKeyTooLong',
  'remote fetch failed or empty': 'remoteFailed',
  'remote returned non-JSON body': 'nonJsonBody',
  'remote response body too large': 'bodyTooLarge',
  'remote returned HTTP 401': 'remoteAuthFailed',
  'remote returned HTTP 403': 'remoteAuthFailed',
  'remote returned HTTP 404': 'remoteNotFound',
  'remote returned HTTP 429': 'remoteRateLimited',
};

/**
 * Statuses that share one message and are therefore matched by prefix.
 *
 * The plugin reports any non-200 status verbatim, so the 5xx family cannot be
 * enumerated; left unmapped, a Chinese user would see the raw English phrase for
 * the two most common setup mistakes (a wrong path and a failing gateway).
 */
const FETCH_REASON_PREFIX_KEYS: readonly [string, string][] = [
  ['remote returned HTTP 5', 'remoteServerError'],
  ['remote returned HTTP 4', 'remoteFailed'],
];

/**
 * Where the options currently on screen came from.
 *
 * `fetched` means the dialog only shows what a remote fetch returned, so an
 * empty list means "nothing fetched yet". `preset` means the dialog preloads
 * the provider defaults, so it can also say "these are presets" and "enter a
 * key to refresh them".
 */
export type OptionSource = 'fetched' | 'preset';

export type Translate = (key: string, values?: Record<string, unknown>) => string;

/** Mirrors the server-side check so an unusable scheme never reaches the fetch RPC. */
export function isHttpApiBase(apiBase: string): boolean {
  return /^https?:\/\/[^/\s]/i.test(apiBase.trim());
}

/**
 * Status hint for a preset provider, given the options currently offered.
 *
 * An unusable provider (no model-list endpoint) is reported before a missing
 * key: no key can make that fetch succeed.
 */
export function getPresetStatusKey(
  preset: VendorPreset | undefined,
  apiKey: string,
  options: readonly string[],
  optionSource: OptionSource,
): string | undefined {
  if (!preset) return undefined;
  if (!preset.models_endpoint)
    return options.length > 0
      ? 'settingsPanel.models.fetchReasons.noEndpoint'
      : 'settingsPanel.models.noPresetModelsNoEndpoint';
  if (preset.models_needs_key && !apiKey.trim())
    return optionSource === 'preset' && options.length > 0
      ? 'settingsPanel.models.fetchReasons.apiKeyRequired'
      : 'settingsPanel.models.noPresetModelsApiKeyRequired';
  if (optionSource === 'preset')
    return options.length > 0 ? 'settingsPanel.models.presetModels' : 'settingsPanel.models.noPresetModels';
  return options.length > 0 ? undefined : 'settingsPanel.models.noPresetModels';
}

/**
 * Status hint for the custom branch.
 *
 * Custom gateways have no preset fallback, so the hint must explain which field
 * is missing instead of offering a generic "refresh" message.
 */
export function getCustomStatusKey(
  apiBase: string,
  apiKey: string,
  options: readonly string[],
): string | undefined {
  if (!apiBase.trim())
    return options.length > 0
      ? 'settingsPanel.models.fetchReasons.apiBaseRequired'
      : 'settingsPanel.models.noCustomModelsApiBaseRequired';
  if (!isHttpApiBase(apiBase)) return 'settingsPanel.models.fetchReasons.apiBaseInvalid';
  if (!apiKey.trim())
    return options.length > 0
      ? 'settingsPanel.models.fetchReasons.apiKeyRequired'
      : 'settingsPanel.models.noCustomModelsApiKeyRequired';
  return options.length > 0 ? undefined : 'settingsPanel.models.noCustomModels';
}

/** Turn a plugin `reason` into localized copy; unknown reasons are shown verbatim. */
export function describeFetchReason(reason: string | undefined, t: Translate): string {
  if (!reason) return t('settingsPanel.models.fetchModelsFailed');
  const reasonKey = FETCH_REASON_KEYS[reason];
  if (reasonKey) return t(`settingsPanel.models.fetchReasons.${reasonKey}`);
  const prefixKey = FETCH_REASON_PREFIX_KEYS.find(([prefix]) => reason.startsWith(prefix))?.[1];
  return prefixKey ? t(`settingsPanel.models.fetchReasons.${prefixKey}`) : reason;
}

/** Dedupe key for a preset fetch: the preset identity plus the key that was used. */
export function getModelFetchKey(preset: VendorPreset, apiKey: string): string {
  return JSON.stringify([preset.plan, preset.vendor_key, apiKey.trim()]);
}

/**
 * Dedupe key for a custom fetch.
 *
 * `protocol` is part of the key because it changes the auth header and the
 * candidate endpoint order, so a list fetched with the other protocol is stale.
 */
export function getCustomModelFetchKey(apiBase: string, apiKey: string, protocol: string): string {
  return JSON.stringify(['custom', apiBase.trim(), apiKey.trim(), protocol]);
}
