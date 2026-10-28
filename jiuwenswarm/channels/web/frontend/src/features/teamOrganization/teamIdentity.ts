// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useEffect, useState } from 'react';
import { createAgentGroupManagementClient } from '../agentManagement/groupClient';
import type { AgentGroupCatalogItem, AgentGroupIdentity } from '../agentManagement/types';
import type { RuntimeTeamInfo } from '../../stores/teamSelectorStore';
import { normalizeTeamGroupIdentity } from '../teamGroupIdentity';

/** Resolve display metadata without changing the session's AgentGroup binding. */
export function resolveOrganizationTeamIdentity(
  team: RuntimeTeamInfo | undefined | null,
  groups: AgentGroupCatalogItem[],
  snapshot?: AgentGroupIdentity | null,
): AgentGroupIdentity | null {
  const groupName = team?.agent_group_name?.trim();
  const savedName = team?.display_name?.trim();
  const stored = normalizeTeamGroupIdentity(team?.team_group_identity) ?? snapshot;
  // Older Teams only saved a display name; require an unambiguous catalog match.
  const candidates = groupName
    ? groups.filter((item) => item.name === groupName || item.id === groupName)
    : savedName
      ? groups.filter((item) => item.name === savedName || item.id === savedName || item.displayName === savedName)
      : [];
  const group = candidates.length === 1 ? candidates[0] : undefined;
  if (group) {
    return { id: group.id, name: group.name, displayName: group.displayName, avatarUrl: group.avatarUrl };
  }
  if (!groupName || !stored) return null;
  return stored.name === groupName || stored.id === groupName ? stored : null;
}

export function resolveOrganizationTeamLabel(
  team: RuntimeTeamInfo | undefined | null,
  groups: AgentGroupCatalogItem[],
  fallback: string,
): string {
  return (
    resolveOrganizationTeamIdentity(team, groups)?.displayName ||
    team?.display_name?.trim() ||
    team?.agent_group_name?.trim() ||
    team?.team_id?.trim() ||
    team?.team_name?.trim() ||
    fallback
  );
}

/** The organization panel loads the installed directory only while it is displaying an organization. */
export function useOrganizationGroupCatalog(enabled: boolean): AgentGroupCatalogItem[] {
  const [groups, setGroups] = useState<AgentGroupCatalogItem[]>([]);
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void createAgentGroupManagementClient()
      .listGroups({ filter: 'local' })
      .then(
        (items) => {
          if (!cancelled) setGroups(items);
        },
        (error: unknown) => {
          if (!cancelled) console.warn('Organization AgentGroup directory failed:', error);
        },
      );
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return groups;
}
