import { webRequest } from '../../services/webClient';
import { withCatalogCache } from '../catalogCache';
import { getAgentManagementLocale } from './locale';
import {
  normalizeAgentGroupDetail,
  normalizeAgentGroupListItem,
  normalizeAgentFileContent,
  normalizeAgentFileTree,
} from './adapter';
import {
  AgentManagementError,
  type AgentGroupListOptions,
  type AgentGroupManagementClient,
  buildAgentGroupCreatePayload,
} from './port';
import type {
  RawAgentGroupDetailPayload,
  RawAgentGroupListPayload,
  RawAgentFileListPayload,
  RawAgentFileReadPayload,
} from './raw';
import type { AgentGroupDraft } from './types';

function readErrorCode(payload: unknown): string | undefined {
  let current: unknown = payload;
  for (let depth = 0; depth < 4; depth += 1) {
    if (!current || typeof current !== 'object') return undefined;
    const record = current as Record<string, unknown>;
    if (typeof record.code === 'string' && record.code.trim()) return record.code;
    current = record.details ?? record.result ?? record.payload;
  }
  return undefined;
}

function rethrowGroupError(error: unknown): never {
  if (error instanceof AgentManagementError) throw error;
  if (error instanceof Error) {
    const webError = error as Error & { code?: string; retriable?: boolean; payload?: unknown };
    throw new AgentManagementError(
      error.message,
      webError.code || readErrorCode(webError.payload) || 'agent_group_request_failed',
      webError.retriable ?? true,
      webError.payload,
    );
  }
  throw new AgentManagementError(String(error), 'agent_group_request_failed', true);
}

export function createLiveAgentGroupManagementClient(): AgentGroupManagementClient {
  return {
    source: 'live',
    async listGroups(options: AgentGroupListOptions = {}) {
      try {
        const filter = options.filter && options.filter !== 'all' ? { filter: options.filter } : {};
        const payload = await webRequest<RawAgentGroupListPayload>('agent_groups.list', {
          ...filter,
          ...(options.cache_mode ? { cache_mode: options.cache_mode } : {}),
        });
        return withCatalogCache(
          (payload.agentGroups || []).map((item) => normalizeAgentGroupListItem(item, getAgentManagementLocale())),
          payload.cache,
        );
      } catch (error) {
        return rethrowGroupError(error);
      }
    },
    async getGroup(id) {
      try {
        const payload = await webRequest<RawAgentGroupDetailPayload>('agent_groups.show', { id });
        if (!payload.group)
          throw new AgentManagementError('AgentGroup detail is empty', 'agent_group_detail_empty', false);
        return normalizeAgentGroupDetail(payload.group, getAgentManagementLocale());
      } catch (error) {
        return rethrowGroupError(error);
      }
    },
    async getGroupFiles(id) {
      try {
        const payload = await webRequest<RawAgentFileListPayload>('agent_groups.file.list', { id });
        return normalizeAgentFileTree(payload.tree);
      } catch (error) {
        return rethrowGroupError(error);
      }
    },
    async getGroupFile(id, relativePath) {
      try {
        const payload = await webRequest<RawAgentFileReadPayload>('agent_groups.file.read', { id, path: relativePath });
        return normalizeAgentFileContent(payload);
      } catch (error) {
        return rethrowGroupError(error);
      }
    },
    async createGroup(draft: AgentGroupDraft) {
      try {
        const payload = await webRequest<{ id?: string }>('agent_groups.create', buildAgentGroupCreatePayload(draft));
        if (!payload?.id)
          throw new AgentManagementError('Imported AgentGroup id is empty', 'agent_group_create_empty', false);
        return { id: payload.id };
      } catch (error) {
        return rethrowGroupError(error);
      }
    },
    async importGroup(path) {
      try {
        const normalizedPath = path.trim();
        if (!normalizedPath)
          throw new AgentManagementError('AgentGroup import path is empty', 'agent_group_import_path_empty', false);
        const payload = await webRequest<{ id?: string }>('agent_groups.import_local', { path: normalizedPath });
        if (!payload?.id)
          throw new AgentManagementError('Imported AgentGroup id is empty', 'agent_group_import_empty', false);
        return { id: payload.id };
      } catch (error) {
        return rethrowGroupError(error);
      }
    },
    async installGroup(id) {
      try {
        await webRequest('agent_groups.install', { id });
      } catch (error) {
        return rethrowGroupError(error);
      }
    },
    async uninstallGroup(id) {
      try {
        return (await webRequest<{ notice?: string }>('agent_groups.uninstall', { id })) || {};
      } catch (error) {
        return rethrowGroupError(error);
      }
    },
  };
}

export function createAgentGroupManagementClient(): AgentGroupManagementClient {
  return createLiveAgentGroupManagementClient();
}
