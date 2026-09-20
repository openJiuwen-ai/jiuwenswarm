import { withCatalogCache, type CatalogCacheMetadata } from '../catalogCache';
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
  normalizeTeamMarketplaceSkill,
} from './adapter';
import type { McpOption, SkillOption } from './types';
import type {
  RawAgentDetailPayload,
  RawAgentFileListPayload,
  RawAgentFileReadPayload,
  RawAgentListPayload,
  RawSkillListPayload,
  RawTeamSkillMarketplacePayload,
} from './raw';

export { AgentManagementError } from './port';
export type { AgentCatalogListOptions, AgentInstallResult, AgentManagementClient, SkillListOptions } from './port';

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

function mergeTeamMarketplaceSkillOptions(
  skillOptions: SkillOption[],
  teamMarketplace: RawTeamSkillMarketplacePayload,
): SkillOption[] {
  const byId = new Map(skillOptions.map((skill) => [skill.id, skill]));
  (teamMarketplace.skills ?? teamMarketplace.items ?? []).forEach((raw) => {
    const normalized = normalizeTeamMarketplaceSkill(raw, byId.get(raw.name?.trim() || ''));
    if (normalized) byId.set(normalized.id, normalized);
  });
  return Array.from(byId.values());
}

async function enrichCatalogTags(items: ReturnType<typeof normalizeAgentTemplateListItem>[]) {
  const missingTags = items.filter((item) => item.tags.length === 0);
  if (missingTags.length === 0) return items;

  const enriched = await Promise.allSettled(
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
  enriched.forEach((result) => {
    if (result.status !== 'fulfilled') return;
    const { id, tags } = result.value;
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
        const payload = await requestEquipmentList<RawAgentListPayload & { cache?: CatalogCacheMetadata }>(
          webRequest,
          'agent_templates.list',
          {
            ...(options.filter ? { filter: options.filter } : {}),
            ...(options.includeTeamCompatibility ? { include_team_compatibility: true } : {}),
          },
        );

        const items = (payload.templates || []).map((item) =>
          normalizeAgentTemplateListItem(item, getAgentManagementLocale()),
        );
        // Cached cards must not wait for remote details. Tag enrichment is an explicit optional operation.
        return withCatalogCache(options.enrichTags === true ? await enrichCatalogTags(items) : items, payload.cache);
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async getDefinition(id) {
      try {
        const payload = await webRequest<RawAgentDetailPayload>('agent_templates.show', { id }, { timeoutMs: 90000 });
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
    async listSkillOptions(options = {}) {
      try {
        const payload = await webRequest<RawSkillListPayload>('skills.list', { with_installed: true });
        const skillOptions = (payload.skills || [])
          .filter((item) => item.source !== 'mcp')
          .map(normalizeSkillOption)
          .filter((item) => item.id.length > 0);
        if (!options.includeTeamMarketplace) return skillOptions;

        const fetchTeamMarketplace = () =>
          webRequest<RawTeamSkillMarketplacePayload>(
            'skills.swarmskillshub.recommend',
            { top_k: 500, cache_mode: 'prefer_cache', plugin_type: 'swarmskill' },
            { timeoutMs: 30000 },
          );

        // Preserve the old awaitable behavior for callers that do not opt into progressive enrichment.
        if (!options.onTeamMarketplaceLoaded) {
          const teamMarketplace = await fetchTeamMarketplace();
          if (teamMarketplace.success === false) return skillOptions;
          return mergeTeamMarketplaceSkillOptions(skillOptions, teamMarketplace);
        }

        // The base list is usable on its own; enrich it asynchronously so a slow Hub never blocks the picker.
        setTimeout(() => {
          void fetchTeamMarketplace()
            .then((teamMarketplace) => {
              if (teamMarketplace.success === false) return;
              options.onTeamMarketplaceLoaded?.(
                mergeTeamMarketplaceSkillOptions(skillOptions, teamMarketplace),
                teamMarketplace.cache,
              );
            })
            .catch(() => {
              // Team marketplace is optional enrichment; keep local and installed skills usable when it fails.
            });
        }, 0);
        return skillOptions;
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async listMcpOptions() {
      try {
        const [marketplaceResult, localResult] = await Promise.allSettled([
          connectorApi.list('builtin'),
          connectorApi.list('local'),
        ]);
        if (localResult.status === 'rejected') throw localResult.reason;
        const marketplace = marketplaceResult.status === 'fulfilled' ? marketplaceResult.value : [];
        const local = localResult.value;
        const byRuntimeName = new Map<string, McpOption>();
        [...marketplace, ...local].forEach((item) => {
          const runtimePackageName = item.runtimePackageName || item.name;
          if (!runtimePackageName) return;
          const next: McpOption = {
            id: runtimePackageName,
            name: item.displayName || runtimePackageName,
            description: item.description || '',
            category: item.category || '',
            integrationType: item.integrationType,
            connectionState: item.connectionState,
            source: item.source,
            runtimePackageName,
            ...(item.hubAssetId ? { hubAssetId: item.hubAssetId } : {}),
            installed: item.installed,
            icon: item.icon,
          };
          const previous = byRuntimeName.get(runtimePackageName);
          byRuntimeName.set(
            runtimePackageName,
            previous
              ? {
                  ...previous,
                  ...next,
                  hubAssetId: next.hubAssetId || previous.hubAssetId,
                }
              : next,
          );
        });
        return Array.from(byRuntimeName.values()).filter((item) => item.id.length > 0);
      } catch (error) {
        return rethrowAgentError(error);
      }
    },
    async installSkill(option) {
      try {
        const hubAssetId = option.hubAssetId?.trim();
        if (option.source === 'teamskillshub' && hubAssetId) {
          const payload = await webRequest<{ success?: boolean; detail?: string }>(
            'skills.teamskillshub.install',
            { asset_id: hubAssetId, force: false, display_name: option.name },
            { timeoutMs: 180000 },
          );
          if (payload?.success === false) {
            throw new AgentManagementError(
              payload.detail || 'Team skill installation failed',
              'skill_install_failed',
              false,
              payload,
            );
          }
          return;
        }
        const spec = option.installSpec?.trim();
        if (!spec) {
          throw new AgentManagementError('Skill install specification is missing', 'skill_install_spec_missing', false);
        }
        const payload = await webRequest<{ success?: boolean; detail?: string }>(
          'skills.install',
          { spec },
          { timeoutMs: 180000 },
        );
        if (payload?.success === false) {
          throw new AgentManagementError(
            payload.detail || 'Skill installation failed',
            'skill_install_failed',
            false,
            payload,
          );
        }
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
    async updateAgent(draft) {
      try {
        await webRequest('agent_templates.update', {
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
    async deleteDefinition(id) {
      try {
        await webRequest('agent_templates.delete', { id });
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
        await webRequest('agent_templates.install', { id }, { timeoutMs: 180000 });
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
