// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useEffect } from 'react';
import { conversationKey, useChatStore, useTeamSelectorStore } from '../../stores';
import { webClient } from '../../services/webClient';
import { normalizeToolCallPayload, normalizeToolResultPayload } from '../tool-events/toolEventNormalizer';
import { expertTeamId } from './conversation';
import { useTeamOrganizationUiEnabled } from './featureConfig';
import { generateUuidV4 } from '../../utils/uuid';

/** Organization streams are isolated from the existing foreground Session lifecycle. */
export function useOrganizationEvents(): void {
  const enabled = useTeamOrganizationUiEnabled();
  useEffect(() => {
    if (!enabled) return;
    const streams = new Map<string, string>();
    const events = [
      'chat.delta',
      'chat.reasoning',
      'chat.final',
      'chat.error',
      'chat.processing_status',
      'chat.tool_call',
      'chat.tool_result',
      'team.task',
      'team.member',
    ];
    const unsubs = events.map((event) =>
      webClient.on(event, ({ payload }) => {
        const sessionId = String(payload.session_id ?? '');
        if (!sessionId) return;
        const teamId = expertTeamId(payload, sessionId);
        if (!teamId) return;
        const key = conversationKey(sessionId, teamId);
        const chat = useChatStore.getState();
        chat.ensureTeamRuntime(sessionId, teamId);
        if (event === 'team.task' || event === 'team.member') {
          if (useTeamSelectorStore.getState().runtimes[sessionId]?.selectedTeamId === teamId) {
            void useTeamSelectorStore.getState().selectTeam(sessionId, teamId);
          }
          return;
        }
        const member = String(payload.member_name ?? 'team_leader');
        const streamKey = `${key}:${member}:${payload.request_id ?? payload.rid ?? ''}`;
        let id = streams.get(streamKey);
        const content = String(payload.content ?? '');
        if ((event === 'chat.delta' || event === 'chat.final') && content) {
          chat.closeReasoning(key);
          if (!id) {
            id = `${payload.role === 'leader' || member === 'team_leader' ? 'team-leader-' : `team-member-${member}@`}${generateUuidV4()}`;
            streams.set(streamKey, id);
            chat.addMessage(key, { id, role: 'system', content: '', timestamp: new Date().toISOString(), teamId });
          }
          const previous = chat.getRuntime(key)?.messages.find((message) => message.id === id)?.content ?? '';
          chat.updateMessage(key, id, {
            content: event === 'chat.final' ? content : previous + content,
            isStreaming: event !== 'chat.final',
          });
        }
        if (event === 'chat.final') {
          if (id) chat.updateMessage(key, id, { isStreaming: false });
          streams.delete(streamKey);
          chat.closeReasoning(key);
          chat.setThinking(key, false);
        } else if (event === 'chat.reasoning') {
          chat.appendReasoning(key, content);
        } else if (event === 'chat.tool_call') {
          chat.closeReasoning(key);
          chat.addToolCall(key, normalizeToolCallPayload(payload));
        } else if (event === 'chat.tool_result') {
          chat.addToolResult(key, normalizeToolResultPayload(payload));
        } else if (event === 'chat.processing_status' && payload.source === 'org_expert_direct') {
          chat.setProcessing(key, payload.is_processing === true);
        } else if (event === 'chat.error') {
          chat.addMessage(key, {
            id: generateUuidV4(),
            role: 'system',
            content: String(payload.error ?? ''),
            timestamp: new Date().toISOString(),
            teamId,
          });
          chat.setProcessing(key, false);
          chat.setThinking(key, false);
        }
      }),
    );
    return () => unsubs.forEach((unsubscribe) => unsubscribe());
  }, [enabled]);
}
