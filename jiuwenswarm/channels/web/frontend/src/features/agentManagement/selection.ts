import type { SkillOption } from './types';

export type SelectionSourceTab = 'local' | 'market';

const LOCAL_SKILL_SOURCES = new Set(['customize', 'local', 'project']);

/** Team skills are identified by the frontmatter kind; skill_type is a backend compatibility fallback. */
export function isTeamSkillOption(skill: Pick<SkillOption, 'kind' | 'skillType'>): boolean {
  return skill.kind === 'swarm-skill' || skill.kind === 'team-skill' || skill.skillType === 'swarm_skill';
}

/** The local tab keeps local-source entries and also includes installed marketplace entries. */
export function isMineSkillOption(skill: Pick<SkillOption, 'source' | 'installed'>): boolean {
  return skill.installed === true || LOCAL_SKILL_SOURCES.has((skill.source || '').trim().toLocaleLowerCase());
}

/** Marketplace skills remain discoverable in the market tab after installation. */
export function isMarketplaceSkillOption(skill: Pick<SkillOption, 'source'>): boolean {
  return !LOCAL_SKILL_SOURCES.has((skill.source || '').trim().toLocaleLowerCase());
}

export function isSkillVisibleInSourceTab(
  skill: Pick<SkillOption, 'source' | 'installed'>,
  tab: SelectionSourceTab,
): boolean {
  return tab === 'market' ? isMarketplaceSkillOption(skill) : isMineSkillOption(skill);
}

type SortableSelection = {
  id?: string;
  installed?: boolean;
  name?: string;
  displayName?: string;
};

function selectionLabel(item: SortableSelection): string {
  return item.displayName?.trim() || item.name?.trim() || item.id?.trim() || '';
}

function compareSelectionLabels(left: SortableSelection, right: SortableSelection): number {
  const labelOrder = selectionLabel(left).localeCompare(selectionLabel(right), undefined, {
    numeric: true,
    sensitivity: 'base',
  });
  return labelOrder || (left.id || '').localeCompare(right.id || '', undefined, { numeric: true });
}

export function sortInstalledFirst<T extends SortableSelection>(items: T[]): T[] {
  return [...items].sort((left, right) => {
    const installedOrder = Number(right.installed === true) - Number(left.installed === true);
    if (installedOrder !== 0) return installedOrder;

    return compareSelectionLabels(left, right);
  });
}

type McpSelection = SortableSelection & {
  connectionState?: string;
};

function mcpReadinessRank(item: McpSelection): number {
  if (item.installed !== true) return 2;
  return item.connectionState === 'connected' ? 0 : 1;
}

export function sortMcpOptions<T extends McpSelection>(items: T[]): T[] {
  return [...items].sort((left, right) => {
    const readinessOrder = mcpReadinessRank(left) - mcpReadinessRank(right);
    return readinessOrder || compareSelectionLabels(left, right);
  });
}
