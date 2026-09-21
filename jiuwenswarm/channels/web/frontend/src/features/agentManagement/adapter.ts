import type {
  AgentCapability,
  AgentCatalogItem,
  AgentConnectionState,
  AgentDetail,
  AgentFileContent,
  AgentGroupCapabilities,
  AgentGroupCatalogItem,
  AgentGroupDetail,
  AgentGroupMember,
  AgentGroupSource,
  AgentSource,
  DefinitionFileEntry,
  SkillOption,
} from './types';
import type {
  RawAgentCapability,
  RawAgentFileEntry,
  RawAgentFileReadPayload,
  RawAgentTag,
  RawAgentTemplateDetail,
  RawAgentTemplateListItem,
  RawAgentGroupCapabilities,
  RawAgentGroupDetail,
  RawAgentGroupListItem,
  RawAgentGroupMember,
  RawLocalizedText,
  RawSkillOption,
  RawTeamSkillMarketplaceItem,
} from './raw';
import { normalizeEquipmentIdentity, normalizeEquipmentSource } from '../equipmentMarketplace';

export type SupportedLocale = 'zh' | 'en';

export function resolveLocalizedText(value: string | RawLocalizedText | undefined, locale: SupportedLocale): string {
  if (typeof value === 'string') {
    return value;
  }
  if (!value) {
    return '';
  }
  return value[locale] || value[locale === 'zh' ? 'en' : 'zh'] || '';
}

export function normalizeAgentSource(source: string | undefined): AgentSource {
  return normalizeEquipmentSource(source, 'local');
}

export function normalizeAgentGroupSource(source: string | undefined): AgentGroupSource {
  if (source === 'hub') return 'hub';
  return source === 'built-in' || source === 'builtin-in' || source === 'builtin' ? 'builtin' : 'local';
}

export function normalizeAgentConnectionState(state: string | undefined): AgentConnectionState {
  return state === 'connected' || state === 'connecting' ? state : 'disconnected';
}

export function isPreviewableFile(relativePath: string): boolean {
  const lowerPath = relativePath.toLowerCase();
  return (
    lowerPath.endsWith('.md') ||
    lowerPath.endsWith('.mdx') ||
    lowerPath.endsWith('.json') ||
    lowerPath.endsWith('.py') ||
    lowerPath.endsWith('.pdf')
  );
}

function normalizeCapability(raw: RawAgentCapability, locale: SupportedLocale): AgentCapability {
  return {
    id: raw.id || resolveLocalizedText(raw.displayName, locale),
    name: resolveLocalizedText(raw.displayName, locale) || raw.id || '',
    description: resolveLocalizedText(raw.displayDescription, locale),
  };
}

function normalizeTags(tags: RawAgentTag[] | undefined, locale: SupportedLocale): AgentCatalogItem['tags'] {
  return (tags || [])
    .map((tag) => {
      const label = resolveLocalizedText(tag, locale);
      return {
        id: tag.id || label,
        label,
      };
    })
    .filter((tag) => tag.label.length > 0);
}

function normalizeAvatarUrl(value: string | undefined): string | null {
  const avatar = value?.trim() || '';
  if (/^(?:https?:|data:image\/)/i.test(avatar)) return avatar;
  return null;
}

function normalizeGroupCapabilities(
  raw: RawAgentGroupCapabilities | undefined,
  source: AgentGroupSource,
  installed: boolean,
): AgentGroupCapabilities {
  return {
    canUse: raw?.canUse ?? installed,
    canInstall: raw?.canInstall ?? !installed,
    canUninstall: raw?.canUninstall ?? (installed || source === 'local'),
    canPreviewFiles: raw?.canPreviewFiles ?? true,
    canEdit: raw?.canEdit === true,
    canPublish: raw?.canPublish === true,
  };
}

function normalizeGroupMember(raw: RawAgentGroupMember, locale: SupportedLocale): AgentGroupMember {
  const id = raw.id?.trim() || 'member';
  return {
    id,
    agentTemplateId: raw.agentTemplateId?.trim() || id,
    displayName: resolveLocalizedText(raw.displayName, locale) || id,
    description: resolveLocalizedText(raw.displayDescription, locale),
    role: raw.role === 'leader' || id === 'leader' ? 'leader' : 'member',
    avatarUrl: normalizeAvatarUrl(raw.avatar),
  };
}

export function normalizeAgentGroupListItem(raw: RawAgentGroupListItem, locale: SupportedLocale): AgentGroupCatalogItem {
  const source = normalizeAgentGroupSource(raw.source);
  const installed = raw.installed === true;
  const members = (raw.members || []).map(member => normalizeGroupMember(member, locale));
  return {
    id: raw.id,
    name: raw.name?.trim() || raw.id,
    displayName: resolveLocalizedText(raw.displayName, locale) || raw.name || raw.id,
    description: resolveLocalizedText(raw.displayDescription, locale),
    category: raw.category || '',
    source,
    installed,
    memberCount: typeof raw.memberCount === 'number' && Number.isFinite(raw.memberCount)
      ? raw.memberCount
      : members.length,
    members,
    skills: (raw.skills || []).map(item => normalizeCapability(item, locale)),
    tags: normalizeTags(raw.tags, locale),
    avatarUrl: normalizeAvatarUrl(raw.avatar),
    capabilities: normalizeGroupCapabilities(raw.capabilities, source, installed),
  };
}

export function normalizeAgentGroupDetail(raw: RawAgentGroupDetail, locale: SupportedLocale): AgentGroupDetail {
  const base = normalizeAgentGroupListItem(raw, locale);
  return {
    ...base,
    version: raw.version || '',
    updatedAt: raw.updatedAt || '',
    details: raw.details || '',
    persona: raw.persona || '',
    leaderId: raw.leaderId || 'leader',
    quickInputs: (raw.quickInputs || [])
      .map(item => resolveLocalizedText(item, locale))
      .filter(item => item.length > 0),
  };
}

export function normalizeAgentTemplateListItem(raw: RawAgentTemplateListItem, locale: SupportedLocale): AgentCatalogItem {
  const source = normalizeAgentSource(raw.source);
  const identity = normalizeEquipmentIdentity(raw);
  return {
    ...identity,
    displayName: resolveLocalizedText(raw.displayName, locale) || raw.id,
    description: resolveLocalizedText(raw.displayDescription, locale),
    category: raw.category || '',
    source,
    installed: raw.installed === true,
    connectionState: normalizeAgentConnectionState(raw.connection_state),
    ...(typeof raw.enabled === 'boolean' ? { enabled: raw.enabled } : {}),
    ...(typeof raw.updateAvailable === 'boolean' ? { updateAvailable: raw.updateAvailable } : {}),
    tags: normalizeTags(raw.tags, locale),
    avatarUrl: raw.avatar ? raw.avatar : null,
    ...(typeof raw.version === 'string' && raw.version ? { version: raw.version } : {}),
    ...(raw.teamCompatible
      ? {
          teamCompatible: {
            leader: raw.teamCompatible.leader === true,
            member: raw.teamCompatible.member === true,
          },
        }
      : {}),
  };
}

export function normalizeAgentTemplateDetail(raw: RawAgentTemplateDetail, locale: SupportedLocale): AgentDetail {
  const base = normalizeAgentTemplateListItem(raw, locale);
  return {
    ...base,
    prompt: raw.prompt || '',
    details: raw.details || '',
    persona: raw.persona || '',
    skills: (raw.skills || []).map((item) => normalizeCapability(item, locale)),
    tools: (raw.tools || []).map((item) => normalizeCapability(item, locale)),
    rails: (raw.rails || []).map((item) => normalizeCapability(item, locale)),
    mcps: (raw.mcps || []).map((item) => normalizeCapability(item, locale)),
    suggestedPrompts: (raw.quickInputs || [])
      .map((item) => resolveLocalizedText(item, locale))
      .filter((item) => item.length > 0),
    pendingConnectors: Array.isArray(raw.pending_connectors)
      ? raw.pending_connectors.filter((item) => typeof item === 'string' && item.length > 0)
      : [],
  };
}

export function normalizeAgentFileTree(entries: RawAgentFileEntry[] | undefined): DefinitionFileEntry[] {
  return (entries || []).map((entry) => {
    const isDirectory = entry.type === 'dir';
    return {
      relativePath: entry.path,
      kind: isDirectory ? 'directory' : 'file',
      ...(entry.visible !== undefined ? { visible: entry.visible } : {}),
      size: entry.size,
      children: isDirectory ? normalizeAgentFileTree(entry.children) : undefined,
      previewable: !isDirectory && (entry.previewable ?? isPreviewableFile(entry.path)),
    };
  });
}

export function normalizeAgentFileContent(raw: RawAgentFileReadPayload): AgentFileContent {
  return {
    relativePath: raw.path || '',
    content: raw.content ?? null,
    downloadUrl: raw.download_url ?? null,
  };
}

export function normalizeSkillOption(raw: RawSkillOption): SkillOption {
  const name = raw.name || raw.display_name || '';
  const source = raw.source?.trim() || '';
  const installed = raw.installed === true;
  const marketplace = raw.marketplace?.trim() || (source && source !== 'builtin' ? source : undefined);
  const installSpec =
    raw.install_spec?.trim() ||
    raw.spec?.trim() ||
    (installed ? undefined : source === 'builtin' ? name : marketplace ? `${name}@${marketplace}` : undefined);
  return {
    id: name,
    name: raw.display_name || name,
    description: raw.description || '',
    source,
    installed,
    ...(raw.kind?.trim() ? { kind: raw.kind.trim() } : {}),
    ...(raw.skill_type?.trim() ? { skillType: raw.skill_type.trim() } : {}),
    ...(marketplace ? { marketplace } : {}),
    ...(installSpec ? { installSpec } : {}),
  };
}

export function normalizeTeamMarketplaceSkill(
  raw: RawTeamSkillMarketplaceItem,
  existing?: SkillOption,
): SkillOption | null {
  const id = raw.name?.trim() || raw.asset_id?.trim() || '';
  const hubAssetId = raw.asset_id?.trim() || '';
  if (!id || !hubAssetId) return null;

  return {
    ...(existing || {
      id,
      name: raw.display_name?.trim() || id,
      description: raw.short_desc?.trim() || raw.description?.trim() || '',
      installed: false,
    }),
    id,
    source: 'teamskillshub',
    pluginType: raw.plugin_type?.trim() || 'swarmskill',
    hubAssetId,
    marketplace: existing?.marketplace || 'teamskillshub',
  };
}
