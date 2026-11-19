import type { ApplicationPluginContribution, ApplicationPluginManifest } from './types';
import { getGatewayApiRoot } from '../utils/env';

const GATEWAY_PLUGIN_PREFIX = '/api/application-plugins';

/** Gateway 把清单挂在 `/api/application-plugins`，浏览器侧根路径由 getGatewayApiRoot 决定。 */
export function applicationPluginsBase(): string {
  return `${getGatewayApiRoot()}/application-plugins`;
}

function publicEntryUrl(entryUrl: string): string {
  if (!entryUrl.startsWith(GATEWAY_PLUGIN_PREFIX)) return entryUrl;
  return `${applicationPluginsBase()}${entryUrl.slice(GATEWAY_PLUGIN_PREFIX.length)}`;
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
    (item.render_mode === 'bundled' || item.render_mode === 'iframe' || item.render_mode === 'none'),
  );
}

export function normalizeApplicationPluginManifest(value: unknown): ApplicationPluginContribution[] {
  if (!value || typeof value !== 'object') return [];
  const manifest = value as Partial<ApplicationPluginManifest>;
  if (manifest.api_version !== 1 || !Array.isArray(manifest.plugins)) return [];
  return manifest.plugins.filter(isContribution).map(plugin => (
    plugin.entry_url
      ? { ...plugin, entry_url: publicEntryUrl(plugin.entry_url) }
      : plugin
  )).sort((left, right) => left.position - right.position);
}

export function enabledApplicationPlugins(plugins: ApplicationPluginContribution[]): ApplicationPluginContribution[] {
  return plugins.filter(plugin => plugin.enabled !== false && plugin.render_mode !== 'none');
}

export async function fetchApplicationPlugins(signal?: AbortSignal): Promise<ApplicationPluginContribution[]> {
  const response = await fetch(applicationPluginsBase(), {
    credentials: 'same-origin',
    signal,
  });
  if (!response.ok) {
    throw new Error(`Application plugin manifest request failed (${response.status})`);
  }
  return normalizeApplicationPluginManifest(await response.json());
}
