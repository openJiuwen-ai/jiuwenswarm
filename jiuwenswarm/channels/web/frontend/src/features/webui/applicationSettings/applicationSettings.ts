/**
 * Host splash settings (Gateway S_10 sections 3 and 4): parse the splash settings and compute the shown and
 * adjusted orders. No React or client dependency; callers pass the request function. Extension settings
 * themselves come through the shared settings catalog (AgentServer S_18).
 */

import type { LocalizedText } from '../catalog/components/localizedText.ts';
import { isRecord } from '../protocol/utils.ts';

export const SPLASH_SETTINGS_METHOD = 'webui.splash.settings';
export const SPLASH_SETTINGS_UPDATE_METHOD = 'webui.splash.settings.update';

/** Titles come from the manifest ``display.name``: one string or ``{zh, en}``, resolved when shown. */
export interface SplashProviderSetting {
  packageId: string;
  title: LocalizedText;
  skippable: boolean;
  maxDurationMs: number;
  enabled: boolean;
}

export interface SplashSettings {
  providers: SplashProviderSetting[];
  customized: boolean;
}


function text(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

function title(value: unknown): value is LocalizedText {
  if (text(value)) return true;
  return isRecord(value) && text(value.zh) && text(value.en);
}

function titleOf(value: LocalizedText): LocalizedText {
  return typeof value === 'string' ? value : { zh: value.zh, en: value.en };
}

export function parseSplashSettings(value: unknown): SplashSettings | null {
  if (!isRecord(value) || !Array.isArray(value.providers) || typeof value.customized !== 'boolean') return null;
  const seen = new Set<string>();
  const providers = value.providers.flatMap((item) => {
    if (!isRecord(item) || !text(item.package_id) || !title(item.title) || seen.has(item.package_id)
      || typeof item.skippable !== 'boolean' || typeof item.enabled !== 'boolean'
      || typeof item.max_duration_ms !== 'number' || !Number.isInteger(item.max_duration_ms)) return [];
    seen.add(item.package_id);
    return [{
      packageId: item.package_id,
      title: titleOf(item.title),
      skippable: item.skippable,
      maxDurationMs: item.max_duration_ms,
      enabled: item.enabled,
    }];
  });
  return { providers, customized: value.customized };
}

/** The order the splash gate will use: without a saved order the appearance provider goes first. */
export function displayedProviders(settings: SplashSettings, preferred?: string | null): SplashProviderSetting[] {
  if (settings.customized || !preferred) return settings.providers;
  const first = settings.providers.filter((provider) => provider.packageId === preferred);
  return [...first, ...settings.providers.filter((provider) => provider.packageId !== preferred)];
}

/** Move one provider up (-1) or down (+1); returns the package ids in the new order, or null at an edge. */
export function movedOrder(providers: readonly SplashProviderSetting[], packageId: string, delta: -1 | 1): string[] | null {
  const ids = providers.map((provider) => provider.packageId);
  const index = ids.indexOf(packageId);
  const next = index + delta;
  if (index < 0 || next < 0 || next >= ids.length) return null;
  [ids[index], ids[next]] = [ids[next], ids[index]];
  return ids;
}

/** The disabled set after turning one provider on or off. */
export function toggledDisabled(providers: readonly SplashProviderSetting[], packageId: string, enabled: boolean): string[] {
  return providers
    .filter((provider) => (provider.packageId === packageId ? !enabled : !provider.enabled))
    .map((provider) => provider.packageId);
}
