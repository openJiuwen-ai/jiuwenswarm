import { getApiBase } from '../../../utils/env';

const PLUGIN_FILES = '/extension-assets/';

/** Published plugin files have their own server-checked authorization in the URL. */
export function isPluginResourceURL(input: string | URL | Request, gatewayUrl: string): boolean {
  const base = new URL(gatewayUrl);
  const url = new URL(input instanceof Request ? input.url : String(input), base);
  return ['http:', 'https:'].includes(url.protocol) && url.origin === base.origin
    && url.pathname.startsWith(base.pathname.replace(/\/$/, '') + PLUGIN_FILES);
}

/** Resolve the public resource protocol without exposing its backend storage location. */
export function resolvePluginResourceURL(source: string, apiBase = getApiBase(), pageUrl?: string): string {
  if (!source.startsWith(PLUGIN_FILES) || !apiBase) return source;
  const url = `${apiBase.replace(/\/$/, '')}${source}`;
  if (pageUrl !== undefined) return new URL(url, pageUrl).href;
  if (typeof window !== 'undefined') return new URL(url, window.location.href).href;
  return url;
}
