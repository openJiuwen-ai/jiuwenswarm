import type { AgentCatalogItem, McpOption, RequestStatus, SkillOption } from './types';

export type SelectionSourceTab = 'local' | 'market';

const LOCAL_SKILL_SOURCES = new Set(['customize', 'local', 'project']);

const TEAM_MARKET_PLUGIN_TYPES = new Set(['swarmskill', 'swarm-skill', 'teamskills', 'team-skill']);

/** Market cards use SkillHub's plugin type; installed/local cards use normalized kind fields. */
export function isTeamSkillOption(
  skill: Pick<SkillOption, 'kind' | 'skillType' | 'pluginType'>,
  sourceTab: SelectionSourceTab,
): boolean {
  if (skill.kind === 'swarm-skill' || skill.kind === 'team-skill' || skill.skillType === 'swarm_skill') return true;
  return sourceTab === 'market' && TEAM_MARKET_PLUGIN_TYPES.has(skill.pluginType || '');
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

export type McpSelectionState = 'available' | 'pending-connection' | 'pending-install';

export function getMcpSelectionState(
  mcp: Pick<McpOption, 'installed' | 'connectionState'>,
): McpSelectionState {
  if (mcp.installed === true && mcp.connectionState === 'connected') return 'available';
  if (mcp.installed === true) return 'pending-connection';
  return 'pending-install';
}

export function isMcpSelectable(mcp: Pick<McpOption, 'installed' | 'connectionState'>): boolean {
  return getMcpSelectionState(mcp) === 'available';
}

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

export function isAgentGroupAgentCompatibilityLoading(
  agent: Pick<AgentCatalogItem, 'source' | 'teamCompatible'> & { installed?: boolean },
  status: RequestStatus,
): boolean {
  return status === 'loading' && agent.installed === true && agent.source === 'hub' && !agent.teamCompatible;
}

export function sortAgentGroupOptions<
  T extends SortableSelection & Pick<AgentCatalogItem, 'source' | 'teamCompatible'>,
>(items: T[], status: RequestStatus): T[] {
  const stateOrder = (item: T) => {
    if (item.installed === true && isAgentGroupAgentCompatibilityLoading(item, status)) return 1;
    return item.installed === true ? 0 : 2;
  };

  return [...items].sort((left, right) => {
    const agentStateOrder = stateOrder(left) - stateOrder(right);
    if (agentStateOrder !== 0) return agentStateOrder;
    return compareSelectionLabels(left, right);
  });
}

export function sortMcpOptions<T extends SortableSelection & Pick<McpOption, 'connectionState'>>(items: T[]): T[] {
  const stateOrder: Record<McpSelectionState, number> = {
    available: 0,
    'pending-connection': 1,
    'pending-install': 2,
  };

  return [...items].sort((left, right) => {
    const mcpStateOrder = stateOrder[getMcpSelectionState(left)] - stateOrder[getMcpSelectionState(right)];
    if (mcpStateOrder !== 0) return mcpStateOrder;

    return compareSelectionLabels(left, right);
  });
}
