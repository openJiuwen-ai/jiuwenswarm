import { webRequest } from './webClient';
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

function reportFailure(payload: SystemPluginReportPayload): string | null {
  const failed = (payload.reports ?? []).find((report) => report.status !== 'SUCCESS');
  if (!failed) return null;
  return failed.error?.message ?? failed.error?.code ?? 'INSTALL_FAILED';
}

export const systemPluginApi = {
  async catalog(scope: 'catalog' | 'mine'): Promise<SystemPluginSummary[]> {
    const result = await webRequest('extensions.v2.catalog', { scope }, { timeoutMs: 30_000 });
    const packages = (result as { packages?: SystemPluginSummary[] } | null)?.packages;
    return Array.isArray(packages) ? packages : [];
  },

  async show(packageId: string): Promise<SystemPluginDetail | null> {
    const result = await webRequest('extensions.v2.show', { package_id: packageId }, { timeoutMs: 30_000 });
    return (result as { package?: SystemPluginDetail } | null)?.package ?? null;
  },

  async files(packageId: string): Promise<DefinitionFileEntry[]> {
    const result = await webRequest<RawAgentFileListPayload>('extensions.v2.file.list', { package_id: packageId });
    return normalizeAgentFileTree(result?.tree);
  },

  async file(packageId: string, path: string): Promise<AgentFileContent> {
    const result = await webRequest<RawAgentFileReadPayload>('extensions.v2.file.read', {
      package_id: packageId,
      path,
    });
    return normalizeAgentFileContent(result ?? { path });
  },

  async install(packageId: string): Promise<void> {
    const result = (await webRequest(
      'extensions.v2.install',
      { package_ids: [packageId] },
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

  async uninstall(packageId: string): Promise<void> {
    const result = (await webRequest(
      'extensions.v2.uninstall',
      { package_id: packageId },
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
