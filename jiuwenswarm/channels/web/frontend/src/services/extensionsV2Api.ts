import { webRequest } from './webClient';
import { withCatalogCache, type CatalogCacheMetadata, type CatalogItems } from '../features/catalogCache';
import { browserClientCapabilities } from '../features/webui/client/browserClient';
import type { SystemPluginDetail, SystemPluginSummary } from '../types/systemPlugin';
import {
  normalizeAgentFileContent,
  normalizeAgentFileTree,
  type AgentFileContent,
  type DefinitionFileEntry,
} from '../features/agentManagement';
import type { RawAgentFileListPayload, RawAgentFileReadPayload } from '../features/agentManagement/raw';

export interface SystemPluginReport {
  package_id?: string;
  status?: string;
  error?: { code?: string; message?: string } | null;
}

export interface SystemPluginReportPayload {
  reports: SystemPluginReport[];
}

// The Gateway waits dependency_timeout (default 300s) + 30s for the AgentServer; the
// client keeps another 30s so the server-side timeout error reaches the UI first.
const INSTALL_TIMEOUT_MS = 360_000;
const READ_TIMEOUT_MS = 30_000;

async function readPluginState(method: string, params: Record<string, unknown>): Promise<unknown> {
  try {
    return await webRequest(method, params, { timeoutMs: READ_TIMEOUT_MS });
  } catch (error) {
    if (errorCode(error) !== 'CONTEXT_CHANGED') {
      throw error;
    }
    if (!(await waitForClientContext())) throw error;
    return webRequest(method, params, { timeoutMs: READ_TIMEOUT_MS });
  }
}

function errorCode(error: unknown): string | undefined {
  if (!error || typeof error !== 'object' || !('code' in error)) return undefined;
  const code = (error as { code?: unknown }).code;
  return typeof code === 'string' ? code : undefined;
}

/**
 * A declarations swap can reject a management call without the package state being
 * wrong; callers resync instead of surfacing a failure. A missing login session is not
 * transient: hiding it would show an empty list or report a rejected toggle as done.
 */
export function isTransientManagementError(error: unknown): boolean {
  return errorCode(error) === 'CONTEXT_CHANGED';
}

/** Resolve once the browser has applied the current client declarations. */
export async function waitForClientContext(): Promise<boolean> {
  const host = browserClientCapabilities;
  if (!host.isChanging()) return !host.isUnavailable();
  return new Promise<boolean>((resolve) => {
    const finish = (available: boolean) => {
      window.clearTimeout(timeoutId);
      unsubscribe();
      resolve(available);
    };
    const unsubscribe = host.subscribe((change) => {
      if (change.phase === 'ready' && !host.isChanging()) finish(!host.isUnavailable());
    });
    const timeoutId = window.setTimeout(() => finish(false), READ_TIMEOUT_MS);
  });
}

function reportFailure(payload: SystemPluginReportPayload): string | null {
  const failed = (payload.reports ?? []).find((report) => report.status !== 'SUCCESS');
  if (!failed) return null;
  return failed.error?.message ?? failed.error?.code ?? 'INSTALL_FAILED';
}

export const systemPluginApi = {
  async catalog(scope: 'catalog' | 'mine', refresh = false): Promise<CatalogItems<SystemPluginSummary>> {
    const result = await readPluginState('extensions.v2.catalog', {
      scope,
      ...(refresh ? { refresh: true } : {}),
    });
    const payload = result as { packages?: SystemPluginSummary[]; cache?: CatalogCacheMetadata } | null;
    return withCatalogCache(Array.isArray(payload?.packages) ? payload.packages : [], payload?.cache);
  },

  async show(packageId: string, version?: string): Promise<SystemPluginDetail | null> {
    const result = await readPluginState('extensions.v2.show', {
      package_id: packageId,
      ...(version ? { version } : {}),
    });
    return (result as { package?: SystemPluginDetail } | null)?.package ?? null;
  },

  async files(packageId: string, version?: string): Promise<DefinitionFileEntry[]> {
    const result = await webRequest<RawAgentFileListPayload>('extensions.v2.file.list', {
      package_id: packageId,
      ...(version ? { version } : {}),
    });
    return normalizeAgentFileTree(result?.tree);
  },

  async file(packageId: string, path: string, version?: string): Promise<AgentFileContent> {
    const result = await webRequest<RawAgentFileReadPayload>('extensions.v2.file.read', {
      package_id: packageId,
      path,
      ...(version ? { version } : {}),
    });
    return normalizeAgentFileContent(result ?? { path });
  },

  async install(packageId: string, version?: string): Promise<void> {
    const result = (await webRequest(
      'extensions.v2.install',
      {
        package_ids: [packageId],
        ...(version ? { version } : {}),
      },
      { timeoutMs: INSTALL_TIMEOUT_MS },
    )) as SystemPluginReportPayload | null;
    const failure = result ? reportFailure(result) : 'INSTALL_FAILED';
    if (failure) throw new Error(failure);
  },

  async importPath(path: string): Promise<void> {
    const result = (await webRequest(
      'extensions.v2.import',
      { path },
      { timeoutMs: INSTALL_TIMEOUT_MS },
    )) as SystemPluginReportPayload | null;
    const failure = result ? reportFailure(result) : 'IMPORT_FAILED';
    if (failure) throw new Error(failure);
  },

  async uninstall(packageId: string, version?: string): Promise<void> {
    const result = (await webRequest(
      'extensions.v2.uninstall',
      { package_id: packageId, ...(version ? { version } : {}) },
      { timeoutMs: INSTALL_TIMEOUT_MS },
    )) as SystemPluginReportPayload | null;
    const failure = result ? reportFailure(result) : 'UNINSTALL_FAILED';
    if (failure) throw new Error(failure);
  },

  async reload(packageId: string): Promise<void> {
    const result = (await webRequest(
      'extensions.v2.reload',
      { package_id: packageId },
      { timeoutMs: INSTALL_TIMEOUT_MS },
    )) as SystemPluginReportPayload | null;
    const failure = result ? reportFailure(result) : 'RELOAD_FAILED';
    if (failure) throw new Error(failure);
  },

  async setEnabled(packageId: string, enable: boolean): Promise<void> {
    const result = (await webRequest(
      'extensions.v2.set_enabled',
      { package_id: packageId, enable },
      { timeoutMs: INSTALL_TIMEOUT_MS },
    )) as SystemPluginReportPayload | null;
    const failure = result ? reportFailure(result) : 'SET_ENABLED_FAILED';
    if (failure) throw new Error(failure);
  },
};
