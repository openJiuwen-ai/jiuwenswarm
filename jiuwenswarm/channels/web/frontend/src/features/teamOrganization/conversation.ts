// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { conversationKey, useChatStore, useTeamSelectorStore } from '../../stores';
import { isTeamOrganizationUiEnabled, useTeamOrganizationUiEnabled } from './featureConfig';
import { generateUuidV4 } from '../../utils/uuid';
import type { MediaItem } from '../../types';
import { finishExpertRequest, isOrganizationProcessing, startExpertRequest } from './processing';

import { ownerTeamId } from './ownerIdentity';
export { ownerTeamId } from './ownerIdentity';

export function expertTeamId(payload: Record<string, unknown>, sessionId: string): string | null {
  const teamId = String(payload.team_id || payload.team_name || '').trim();
  if (!teamId || teamId === ownerTeamId(sessionId)) return null;
  const source = String(payload.source ?? '');
  // Root follow-up events can originate from any Team. Preserve legacy
  // routing when disabled; when enabled, Team identity determines the owner.
  if (source.startsWith('org_root_') && !isTeamOrganizationUiEnabled()) return null;
  const teams = useTeamSelectorStore.getState().runtimes[sessionId]?.teams ?? [];
  if (teams.find((team) => team.team_id === teamId)?.is_owner) return null;
  return source.startsWith('org_') || teams.some((team) => team.team_id === teamId) ? teamId : null;
}

export function selectedExpertTeamId(sessionId: string): string | null {
  if (!isTeamOrganizationUiEnabled()) return null;
  const runtime = useTeamSelectorStore.getState().runtimes[sessionId];
  const selected = runtime?.teams.find((team) => team.team_id === runtime.selectedTeamId);
  return selected && !selected.is_owner && selected.team_id !== ownerTeamId(sessionId) ? selected.team_id : null;
}

export function organizationOwnerStopTarget(sessionId: string): string | null {
  if (!isTeamOrganizationUiEnabled()) return null;
  const runtime = useTeamSelectorStore.getState().runtimes[sessionId];
  const owner = runtime?.teams.find((team) => team.is_owner);
  const ownerId = owner?.team_id || ownerTeamId(sessionId);
  const hasOrganization = Boolean(owner?.organization_id) || isOrganizationProcessing(sessionId);
  const isProcessing = Boolean(useChatStore.getState().getRuntime(sessionId)?.isProcessing);
  if (!ownerId || !hasOrganization || !isProcessing) return null;
  return !runtime?.selectedTeamId || runtime.selectedTeamId === ownerId ? ownerId : null;
}

export function useOrganizationConversationKey(sessionId: string | null): string {
  const enabled = useTeamOrganizationUiEnabled();
  const selectedId = useTeamSelectorStore((state) => (sessionId ? state.runtimes[sessionId]?.selectedTeamId : null));
  const teams = useTeamSelectorStore((state) => (sessionId ? state.runtimes[sessionId]?.teams : undefined));
  const team = teams?.find((item) => item.team_id === selectedId);
  return conversationKey(sessionId ?? '', enabled && team && !team.is_owner ? team.team_id : null);
}

export async function sendExpertMessage(
  content: string,
  sessionId: string,
  mediaItems: MediaItem[] = [],
  attachments: Record<string, unknown> = {},
): Promise<boolean | null> {
  const teamId = selectedExpertTeamId(sessionId);
  if (!teamId) return null;
  const { webClient } = await import('../../services/webClient');
  const key = conversationKey(sessionId, teamId);
  const chat = useChatStore.getState();
  chat.ensureTeamRuntime(sessionId, teamId);
  chat.addMessage(key, {
    id: generateUuidV4(),
    role: 'user',
    content,
    mediaItems,
    timestamp: new Date().toISOString(),
    teamId,
  });
  let requestId = generateUuidV4();
  startExpertRequest(key, requestId);
  try {
    await webClient.request(
      'chat.send',
      {
        ...attachments,
        session_id: sessionId,
        mode: 'team',
        content,
        target_team_id: teamId,
      },
      {
        onRequestId: (id) => {
          startExpertRequest(key, id);
          finishExpertRequest(key, requestId);
          requestId = id;
        },
      },
    );
    return true;
  } catch (error) {
    finishExpertRequest(key, requestId);
    throw error;
  }
}
