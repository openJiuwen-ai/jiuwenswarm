import type {
  AgentCapability,
  AgentCatalogItem,
  AgentConnectionState,
  AgentDetail,
  AgentFileContent,
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
  RawLocalizedText,
  RawSkillOption,
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

export function normalizeAgentConnectionState(state: string | undefined): AgentConnectionState {
  return state === 'connected' || state === 'connecting' ? state : 'disconnected';
}

export function isPreviewableFile(relativePath: string): boolean {
  const lowerPath = relativePath.toLowerCase();
  return (
    lowerPath.endsWith('.md') || lowerPath.endsWith('.mdx') || lowerPath.endsWith('.json') || lowerPath.endsWith('.py')
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

export function normalizeAgentTemplateListItem(
  raw: RawAgentTemplateListItem,
  locale: SupportedLocale,
): AgentCatalogItem {
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
  };
}

export function normalizeAgentTemplateDetail(raw: RawAgentTemplateDetail, locale: SupportedLocale): AgentDetail {
  const base = normalizeAgentTemplateListItem(raw, locale);
  return {
    ...base,
    prompt: raw.prompt || '',
    details: raw.details || '',
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
      previewable: !isDirectory && isPreviewableFile(entry.path),
    };
  });
}

export function normalizeAgentFileContent(raw: RawAgentFileReadPayload): AgentFileContent {
  return {
    relativePath: raw.path || '',
    content: raw.content || '',
  };
}

export function normalizeSkillOption(raw: RawSkillOption): SkillOption {
  const name = raw.name || raw.display_name || '';
  return {
    id: name,
    name: raw.display_name || name,
    description: raw.description || '',
  };
}
