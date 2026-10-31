import { matchesAssetName } from '../assetNameSearch';
import type { AgentCatalogItem, AgentDetail, AgentGroupCatalogItem, AgentGroupDetail, DefinitionFileEntry } from './types';

export type CatalogScope = 'catalog' | 'mine';

export type CatalogViewModel = {
  items: AgentCatalogItem[];
  totalItems: number;
};

export type GroupCatalogScope = 'catalog' | 'mine';

export type GroupCatalogViewModel = {
  items: AgentGroupCatalogItem[];
  totalItems: number;
};

/** Category tabs are derived from the loaded data (top N by frequency); the rest falls into "other". */
export const OTHER_CATEGORY = 'Other';

export function collectTopCategories(categories: readonly string[], topN = 6): string[] {
  const counts = new Map<string, number>();
  for (const raw of categories) {
    const category = raw.trim();
    if (!category) continue;
    counts.set(category, (counts.get(category) ?? 0) + 1);
  }
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([key]) => key)
    .slice(0, topN);
}

function matchesDynamicCategory(category: string, itemCategory: string, topCategories: ReadonlySet<string>): boolean {
  if (!category) return true;
  const normalized = itemCategory.trim();
  if (category === OTHER_CATEGORY) return !topCategories.has(normalized);
  return normalized === category;
}

export function findFirstPreviewableFile(entries: DefinitionFileEntry[]): string | null {
  const preferred = entries.find(
    (entry) =>
      entry.visible !== false &&
      entry.kind === 'file' &&
      entry.relativePath.toLowerCase().startsWith('persona/') &&
      entry.previewable,
  );
  if (preferred) return preferred.relativePath;

  for (const entry of entries) {
    if (entry.visible === false) continue;
    if (entry.kind === 'file' && entry.previewable) return entry.relativePath;
    const nested = entry.children ? findFirstPreviewableFile(entry.children) : null;
    if (nested) return nested;
  }
  return null;
}

export function mergeAgentDetailWithCatalog(
  detail: AgentDetail,
  catalogItem: AgentCatalogItem | undefined,
): AgentDetail {
  if (!catalogItem) return detail;
  return {
    ...detail,
    id: catalogItem.id,
    runtimePackageName: catalogItem.runtimePackageName,
    ...(catalogItem.hubAssetId ? { hubAssetId: catalogItem.hubAssetId } : {}),
    displayName: catalogItem.displayName,
    category: catalogItem.category,
    source: catalogItem.source,
    installed: catalogItem.installed,
    connectionState: catalogItem.connectionState,
    ...(catalogItem.enabled !== undefined ? { enabled: catalogItem.enabled } : {}),
    ...(catalogItem.updateAvailable !== undefined ? { updateAvailable: catalogItem.updateAvailable } : {}),
    tags: detail.tags.length > 0 ? detail.tags : catalogItem.tags,
    avatarUrl: detail.avatarUrl || catalogItem.avatarUrl,
    ...(catalogItem.version ? { version: catalogItem.version } : {}),
  };
}

export function mergeAgentGroupDetailWithCatalog(
  detail: AgentGroupDetail,
  catalogItem: AgentGroupCatalogItem | undefined,
): AgentGroupDetail {
  if (!catalogItem) return detail;
  return {
    ...detail,
    id: catalogItem.id,
    name: catalogItem.name,
    displayName: catalogItem.displayName,
    category: catalogItem.category,
    source: catalogItem.source,
    installed: catalogItem.installed,
    memberCount: catalogItem.memberCount,
    members: detail.members.length > 0 ? detail.members : catalogItem.members,
    skills: detail.skills.length > 0 ? detail.skills : catalogItem.skills,
    tags: detail.tags.length > 0 ? detail.tags : catalogItem.tags,
    avatarUrl: detail.avatarUrl || catalogItem.avatarUrl,
    capabilities: catalogItem.capabilities,
  };
}

export function buildCatalogViewModel(
  catalog: AgentCatalogItem[],
  options: {
    scope: CatalogScope;
    category: string;
    query: string;
    /** Category values shown as tabs (from collectTopCategories); values outside it fall into "other". */
    categories?: ReadonlySet<string>;
  },
): CatalogViewModel {
  const query = options.query.trim().toLocaleLowerCase();
  const topCategories = options.categories ?? new Set<string>();
  const items = catalog.filter((item) => {
    if (options.scope === 'catalog' && item.source !== 'builtin' && item.source !== 'hub') {
      return false;
    }
    if (options.scope === 'mine' && item.source !== 'local' && !item.installed) {
      return false;
    }
    if (options.scope === 'catalog' && !matchesDynamicCategory(options.category, item.category, topCategories)) {
      return false;
    }
    if (!query) {
      return true;
    }
    if (options.scope === 'mine') return matchesAssetName(query, [item.displayName]);
    return `${item.displayName} ${item.description} ${item.category}`.toLocaleLowerCase().includes(query);
  });
  return {
    items,
    totalItems: items.length,
  };
}

export function buildGroupCatalogViewModel(
  catalog: AgentGroupCatalogItem[],
  options: {
    scope: GroupCatalogScope;
    category: string;
    query: string;
    installation?: 'all' | 'installed' | 'uninstalled';
    /** Category values shown as tabs (from collectTopCategories); values outside it fall into "other". */
    categories?: ReadonlySet<string>;
  },
): GroupCatalogViewModel {
  const query = options.query.trim().toLocaleLowerCase();
  const topCategories = options.categories ?? new Set<string>();
  const filtered = catalog.filter(item => {
    if (options.scope === 'catalog' && item.source !== 'builtin' && item.source !== 'hub') return false;
    if (options.scope === 'mine' && item.source !== 'local' && !item.installed) return false;
    if (
      options.installation &&
      options.installation !== 'all' &&
      item.installed !== (options.installation === 'installed')
    )
      return false;
    if (options.scope === 'catalog' && !matchesDynamicCategory(options.category, item.category, topCategories)) return false;
    if (!query) return true;
    if (options.scope === 'mine') return matchesAssetName(query, [item.displayName]);
    const tags = item.tags.map(tag => tag.label).join(' ');
    return `${item.name} ${item.displayName} ${item.description} ${item.category} ${tags}`
      .toLocaleLowerCase()
      .includes(query);
  });
  return {
    items: filtered,
    totalItems: filtered.length,
  };
}
