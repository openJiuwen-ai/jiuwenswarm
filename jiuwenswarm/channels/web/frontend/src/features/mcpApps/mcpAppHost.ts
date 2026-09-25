/**
 * MCP Apps (io.modelcontextprotocol/ui) host helpers.
 *
 * The backend exposes three RPCs (see jiuwenswarm/server/runtime/mcp/apps.py):
 * mcp_app.list_tools / mcp_app.read_resource / mcp_app.call_tool. This module
 * wraps them with caches; the iframe document is built in mcpAppDocument.ts.
 */
import { webRequest } from '../../services/webClient';
import { getApiBase } from '../../utils/env';
import { parseAppResource, type McpAppResource } from './mcpAppDocument';

export { buildAppDocument } from './mcpAppDocument';
export type { McpAppResource } from './mcpAppDocument';

export interface McpAppToolSpec {
  name: string;
  annotations?: { readOnlyHint?: boolean; title?: string };
  _meta?: Record<string, unknown>;
}

const resourceCache = new Map<string, Promise<McpAppResource>>();
let sandboxUrlPromise: Promise<string | null> | null = null;

/**
 * Absolute URL of the sandbox proxy page served on the backend's own origin
 * (distinct from the UI origin), or null when unavailable — callers then fall
 * back to a srcdoc iframe. Resolved once per page load.
 */
export function loadSandboxUrl(): Promise<string | null> {
  if (!sandboxUrlPromise) {
    sandboxUrlPromise = fetch(`${getApiBase()}/api/v1/mcp-app/sandbox`, { cache: 'no-store' })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload: { url?: unknown } | null) => {
        const url = typeof payload?.url === 'string' ? payload.url : null;
        // Only usable when it is a different origin from this UI.
        return url && new URL(url).origin !== window.location.origin ? url : null;
      })
      .catch(() => null);
  }
  return sandboxUrlPromise;
}
const toolsCache = new Map<string, Promise<McpAppToolSpec[]>>();

export function loadAppResource(server: string, uri: string): Promise<McpAppResource> {
  const key = `${server}\n${uri}`;
  let pending = resourceCache.get(key);
  if (!pending) {
    pending = webRequest<unknown>('mcp_app.read_resource', { server, uri }, { timeoutMs: 60000 })
      .then((result) => parseAppResource(result, uri));
    pending.catch(() => resourceCache.delete(key));
    resourceCache.set(key, pending);
  }
  return pending;
}

export function loadAppTools(server: string): Promise<McpAppToolSpec[]> {
  let pending = toolsCache.get(server);
  if (!pending) {
    pending = webRequest<{ tools?: McpAppToolSpec[] }>('mcp_app.list_tools', { server }, { timeoutMs: 60000 })
      .then((payload) => payload.tools ?? []);
    pending.catch(() => toolsCache.delete(server));
    toolsCache.set(server, pending);
  }
  return pending;
}

export function callAppTool(
  server: string,
  tool: string,
  args: Record<string, unknown> | undefined,
): Promise<Record<string, unknown>> {
  return webRequest<Record<string, unknown>>(
    'mcp_app.call_tool',
    { server, tool, arguments: args ?? {} },
    { timeoutMs: 120000 },
  );
}

/** App-initiated calls to tools not marked read-only need the user's approval. */
export async function appToolNeedsApproval(server: string, tool: string): Promise<boolean> {
  const spec = (await loadAppTools(server)).find((item) => item.name === tool);
  return spec?.annotations?.readOnlyHint !== true;
}

/** Open an http(s) link from an app in the system browser. */
export async function openAppLink(url: string): Promise<boolean> {
  let parsed: URL;
  try {
    parsed = new URL(url);
  } catch {
    return false;
  }
  if (parsed.protocol !== 'https:' && parsed.protocol !== 'http:') return false;
  // Desktop shells (Electron and pywebview) expose open_app_link; their
  // open_external_url is restricted to SkillHub OAuth and would refuse.
  const desktopOpen = window.pywebview?.api?.open_app_link;
  if (desktopOpen) return Boolean(await desktopOpen(parsed.href));
  if (window.__JIUWEN_DESKTOP__) return false;
  return Boolean(window.open(parsed.href, '_blank', 'noopener,noreferrer'));
}
