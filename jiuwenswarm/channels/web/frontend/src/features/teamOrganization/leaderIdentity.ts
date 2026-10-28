// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useMemo } from 'react';
import { useSessionStore } from '../../stores/sessionStore';
import { useTeamSelectorStore, type RuntimeTeamInfo, type TeamSnapshotMember } from '../../stores/teamSelectorStore';
import { isTeamLeaderMember } from '../../utils/teamMemberAvatar';
import type { TeamLeaderIdentity } from '../teamLeaderIdentity';
import { useOrganizationConversationKey } from './conversation';

/** Resolve only this Team's identity; runtime labels do not create an AgentGroup binding. */
function resolveOrganizationLeaderIdentity(
  team: RuntimeTeamInfo | undefined,
  members: readonly Pick<TeamSnapshotMember, 'member_id' | 'name'>[] | undefined,
  identity: TeamLeaderIdentity | null,
): TeamLeaderIdentity | null {
  if (identity) return identity;
  const leader = members?.find((member) => member.member_id === team?.leader_id)
    ?? members?.find((member) => isTeamLeaderMember(member.member_id));
  const displayName = leader?.name?.trim() || team?.display_name?.trim() || team?.team_id?.trim();
  // This is a display-only identity, without inventing an expert template for runtime Teams.
  return displayName ? { agentTemplateId: '', displayName } : null;
}

export function useConversationLeaderIdentity(sessionId: string | null): TeamLeaderIdentity | null {
  const conversationId = useOrganizationConversationKey(sessionId);
  const identity = useSessionStore((state) => state.runtimes[conversationId]?.teamLeaderIdentity ?? null);
  const members = useSessionStore((state) => state.runtimes[conversationId]?.teamMembers);
  const team = useTeamSelectorStore((state) => {
    const runtime = state.runtimes[sessionId ?? ''];
    return runtime?.teams.find((item) => item.team_id === runtime.selectedTeamId);
  });
  return useMemo(
    () => conversationId === (sessionId ?? '') ? identity : resolveOrganizationLeaderIdentity(team, members, identity),
    [conversationId, sessionId, identity, team, members],
  );
}
