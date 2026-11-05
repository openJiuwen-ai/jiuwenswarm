/**
 * Application settings (Gateway S_10 sections 3 and 4): parse the settings pages and splash settings and
 * compute the shown and adjusted orders. No React or client dependency; callers pass the request function.
 */

export const SETTINGS_PAGE_SLOT_ID = 'application.settings.page';
export const SETTINGS_PAGE_INSTANCE_KEY = 'main';
export const SETTINGS_PAGES_METHOD = 'webui.settings.pages';
export const SPLASH_SETTINGS_METHOD = 'webui.splash.settings';
export const SPLASH_SETTINGS_UPDATE_METHOD = 'webui.splash.settings.update';

export interface ApplicationSettingsPage {
  packageId: string;
  title: string;
}

export interface SplashProviderSetting {
  packageId: string;
  title: string;
  skippable: boolean;
  maxDurationMs: number;
  enabled: boolean;
}

export interface SplashSettings {
  providers: SplashProviderSetting[];
  customized: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function text(value: unknown): value is string {
  return typeof value === 'string' && value.length > 0;
}

export function parseSettingsPages(value: unknown): ApplicationSettingsPage[] {
  if (!isRecord(value) || !Array.isArray(value.pages)) return [];
  const seen = new Set<string>();
  return value.pages.flatMap((item) => {
    if (!isRecord(item) || !text(item.package_id) || !text(item.title) || seen.has(item.package_id)) return [];
    seen.add(item.package_id);
    return [{ packageId: item.package_id, title: item.title }];
  });
}

export function parseSplashSettings(value: unknown): SplashSettings | null {
  if (!isRecord(value) || !Array.isArray(value.providers) || typeof value.customized !== 'boolean') return null;
  const seen = new Set<string>();
  const providers = value.providers.flatMap((item) => {
    if (!isRecord(item) || !text(item.package_id) || !text(item.title) || seen.has(item.package_id)
      || typeof item.skippable !== 'boolean' || typeof item.enabled !== 'boolean'
      || typeof item.max_duration_ms !== 'number' || !Number.isInteger(item.max_duration_ms)) return [];
    seen.add(item.package_id);
    return [{
      packageId: item.package_id,
      title: item.title,
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
