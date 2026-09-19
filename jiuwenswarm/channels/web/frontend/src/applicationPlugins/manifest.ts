import type { ApplicationPluginContribution, ApplicationPluginManifest } from './types';

/**
 * Something changed what the server would answer with -- a package was
 * installed or removed, a plugin was switched on or off. Anything holding the
 * manifest re-reads it rather than patching its own copy, so the nav rail, the
 * 应用插件 cards and every gate that reads a plugin's enabled flag stay one fact.
 */
export const APPLICATION_PLUGINS_CHANGED_EVENT = 'jiuwen:application-plugins-changed';

export function announceApplicationPluginsChanged(): void {
  window.dispatchEvent(new Event(APPLICATION_PLUGINS_CHANGED_EVENT));
}

function isContribution(value: unknown): value is ApplicationPluginContribution {
  if (!value || typeof value !== 'object') return false;
  const item = value as Partial<ApplicationPluginContribution>;
  return Boolean(
    item.plugin_id &&
    item.id &&
    item.nav_key &&
    item.title &&
    (item.enabled === undefined || typeof item.enabled === 'boolean') &&
    // Identity is optional: a plugin that ships neither an icon nor a
    // localized description is still a valid contribution and renders with
    // the generic glyph. Only a wrongly-typed value is rejected, so a bad
    // icon can never reach an <img src>.
    (item.icon === undefined || typeof item.icon === 'string') &&
    (item.logo === undefined || typeof item.logo === 'string') &&
    (item.description === undefined || typeof item.description === 'string') &&
    (item.description_i18n_key === undefined || typeof item.description_i18n_key === 'string') &&
    (item.render_mode === 'bundled' || item.render_mode === 'iframe' || item.render_mode === 'none'),
  );
}

export function normalizeApplicationPluginManifest(value: unknown): ApplicationPluginContribution[] {
  if (!value || typeof value !== 'object') return [];
  const manifest = value as Partial<ApplicationPluginManifest>;
  if (manifest.api_version !== 1 || !Array.isArray(manifest.plugins)) return [];
  return manifest.plugins.filter(isContribution).sort((left, right) => left.position - right.position);
}

export function enabledApplicationPlugins(plugins: ApplicationPluginContribution[]): ApplicationPluginContribution[] {
  return plugins.filter(plugin => plugin.enabled !== false && plugin.render_mode !== 'none');
}

export async function fetchApplicationPlugins(signal?: AbortSignal): Promise<ApplicationPluginContribution[]> {
  const response = await fetch('/api/application-plugins', {
    credentials: 'same-origin',
    signal,
  });
  if (!response.ok) {
    throw new Error(`Application plugin manifest request failed (${response.status})`);
  }
  return normalizeApplicationPluginManifest(await response.json());
}
