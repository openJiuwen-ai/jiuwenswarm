import { connectorApi } from '../../services/connectorApi';
import { webRequest } from '../../services/webClient';
import { requestEquipmentList } from '../equipmentListRequest';
import {
  AgentInstallPendingError,
  AgentManagementError,
  type AgentCatalogListOptions,
  type AgentManagementClient,
} from './port';
import { getAgentManagementLocale } from './locale';
import { resolveAgentTagPayload } from './tagOptions';
import { invalidateAgentCatalog } from '../../stores/agentCatalogStore';
import {
  normalizeAgentFileContent,
  normalizeAgentFileTree,
  normalizeAgentTemplateDetail,
  normalizeAgentTemplateListItem,
  normalizeSkillOption,
} from './adapter';
import type {
  RawAgentDetailPayload,
  RawAgentFileListPayload,
  RawAgentFileReadPayload,
  RawAgentListPayload,
  RawSkillListPayload,
} from './raw';

export { AgentManagementError } from './port';
export type { AgentCatalogListOptions, AgentInstallResult, AgentManagementClient } from './port';

function rethrowAgentError(error: unknown): never {
  if (error instanceof AgentManagementError) {
    throw error;
  }
  if (error instanceof Error) {
    const webError = error as Error & { code?: string; retriable?: boolean; payload?: unknown };
    throw new AgentManagementError(
      error.message,
      webError.code || 'agent_management_request_failed',
      webError.retriable ?? true,
      webError.payload,
    );
  }
  throw new AgentManagementError(String(error));
}

function extractPendingConnectors(error: unknown): string[] | undefined {
  const payload = error instanceof AgentManagementError ? error.payload : undefined;
  if (payload && typeof payload === 'object') {
    const pending = (payload as { pending_connectors?: unknown }).pending_connectors;
    if (Array.isArray(pending) && pending.every((item) => typeof item === 'string') && pending.length > 0)
      return pending;
  }

  // The current Gateway error projection keeps the contract's human-readable
  // message but drops the failed payload. Preserve the install flow when that
  // projection is encountered; unrelated errors do not match this exact form.
  const message = error instanceof Error ? error.message : String(error || '');
  const names = /^connector not connected:\s*(.+)$/i
    .exec(message.trim())?.[1]
    ?.split(',')
    .map((name) => name.trim())
    .filter(Boolean);
  return names && names.length > 0 ? names : undefined;
}

async function enrichCatalogTags(items: ReturnType<typeof normalizeAgentTemplateListItem>[]) {
  const missingTags = items.filter((item) => item.tags.length === 0);
  if (missingTags.length === 0) return items;

  const enriched = await Promise.all(
    missingTags.map(async (item) => {
      const payload = await webRequest<RawAgentDetailPayload>('agent_templates.show', { id: item.id });
      if (!payload.template) {
        throw new AgentManagementError('Agent detail is empty', 'agent_detail_empty', false);
      }
      return {
        id: item.id,
        tags: normalizeAgentTemplateDetail(payload.template, getAgentManagementLocale()).tags,
      };
    }),
  );
  const tagsById = new Map<string, ReturnType<typeof normalizeAgentTemplateListItem>['tags']>();
  enriched.forEach(({ id, tags }) => {
    if (tags.length > 0) tagsById.set(id, tags);
  });
  return items.map((item) => {
    const tags = tagsById.get(item.id);
    return tags ? { ...item, tags } : item;
  });
}

export function createLiveAgentManagementClient(): AgentManagementClient {
  return {
    source: 'live',
    async listCatalog(options: AgentCatalogListOptions = {}) {
      try {
        const payload = await requestEquipmentList<RawAgentListPayload>(webRequest, 'agent_templates.list', {
          ...(options.filter ? { filter: options.filter } : {}),
        });
        const items = (payload.templates || []).map((item) =>
          normalizeAgentTemplateListItem(item, getAgentManagementLocale()),
        );
        return options.enrichTags === false ? items : enrichCatalogTags(items);
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async getDefinition(id) {
      try {
        const payload = await webRequest<RawAgentDetailPayload>('agent_templates.show', { id });
        if (!payload.template) {
          throw new AgentManagementError('Agent detail is empty', 'agent_detail_empty', false);
        }
        return normalizeAgentTemplateDetail(payload.template, getAgentManagementLocale());
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async getDefinitionFiles(id) {
      try {
        const payload = await webRequest<RawAgentFileListPayload>('agent_templates.file.list', { id });
        return normalizeAgentFileTree(payload.tree);
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async getDefinitionFile(id, relativePath) {
      try {
        const payload = await webRequest<RawAgentFileReadPayload>('agent_templates.file.read', {
          id,
          path: relativePath,
        });
        return normalizeAgentFileContent(payload);
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async listSkillOptions() {
      try {
        const payload = await webRequest<RawSkillListPayload>('skills.list', { with_installed: true });
        return (payload.skills || [])
          .filter((item) => item.installed === true && item.source !== 'mcp')
          .map(normalizeSkillOption)
          .filter((item) => item.id.length > 0);
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async listMcpOptions() {
      try {
        const local = await connectorApi.list('local');
        return local
          .map((item) => ({
            id: item.name,
            name: item.displayName || item.name,
            description: item.description || '',
            category: item.category || '',
            integrationType: item.integrationType,
            connectionState: item.connectionState,
            source: item.source,
          }))
          .filter((item) => item.id.length > 0);
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async createAgent(draft) {
      try {
        await webRequest('agent_templates.create', {
          id: draft.id,
          name: draft.name,
          description: draft.description,
          persona: draft.persona,
          tags: resolveAgentTagPayload(draft.tagIds, draft.customTags),
          skills: draft.skillRefs,
          mcps: draft.mcpRefs,
          quickInputs: draft.suggestedPrompts.filter((prompt) => prompt.trim().length > 0),
        });
        invalidateAgentCatalog();
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async importAgentTemplate(path) {
      try {
        const payload = await webRequest<{ id?: string }>('agent_templates.import_local', { path });
        if (!payload?.id) {
          throw new AgentManagementError('Imported Agent id is empty', 'agent_import_empty', false);
        }
        invalidateAgentCatalog();
        return { id: payload.id };
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async installDefinition(id) {
      try {
        await webRequest('agent_templates.install', { id });
        invalidateAgentCatalog();
        return { kind: 'ok' };
      } catch (error) {
        const pendingConnectors = extractPendingConnectors(error);
        if (pendingConnectors) {
          throw new AgentInstallPendingError(error instanceof Error ? error.message : String(error), pendingConnectors);
        }
        return rethrowAgentError(error);
      }
    },
    async uninstallDefinition(id) {
      try {
        const result = (await webRequest<{ notice?: string }>('agent_templates.uninstall', { id })) || {};
        invalidateAgentCatalog();
        return result;
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
  };
}

export function createAgentManagementClient(): AgentManagementClient {
  return createLiveAgentManagementClient();
}
