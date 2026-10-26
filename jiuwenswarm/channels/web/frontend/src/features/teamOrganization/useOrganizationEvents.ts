// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useEffect } from 'react';
import { conversationKey, useChatStore, useTeamSelectorStore } from '../../stores';
import { webClient } from '../../services/webClient';
import { normalizeToolCallPayload, normalizeToolResultPayload } from '../tool-events/toolEventNormalizer';
import { expertTeamId } from './conversation';
import { useTeamOrganizationUiEnabled } from './featureConfig';
import { generateUuidV4 } from '../../utils/uuid';
import { expertProcessingSnapshot, finishExpertRequest, replaceExpertRequests, startExpertRequest } from './processing';

/** Organization streams are isolated from the existing foreground Session lifecycle. */
export function useOrganizationEvents(): void {
  const enabled = useTeamOrganizationUiEnabled();
  useEffect(() => {
    if (!enabled) return;
    const streams = new Map<string, string>();
    const pendingKeys = new Set<string>();
    let disposed = false;
    let syncing = false;
    const synchronize = async () => {
      if (syncing || webClient.getState() !== 'ready') return;
      const snapshot = expertProcessingSnapshot();
      const keys = new Set([...pendingKeys, ...snapshot.keys]);
      if (!keys.size) return;
      syncing = true;
      try {
        const sessions = [...new Set([...keys].map((key) => key.split('::')[0]))];
        const results = await Promise.all(sessions.map(async (sessionId) => ({
          sessionId,
          result: await webClient.request<{ expert_requests?: { team_id: string; request_id: string }[] }>(
            'team.list', { session_id: sessionId },
          ),
        })));
        // A new send or terminal received while querying wins over an older snapshot.
        if (disposed || webClient.getState() !== 'ready' || expertProcessingSnapshot().revision !== snapshot.revision) return;
        for (const { sessionId, result } of results) {
          if (!Array.isArray(result.expert_requests)) continue;
          for (const key of keys) {
            if (key.split('::')[0] !== sessionId) continue;
            const teamId = key.slice(sessionId.length + 2);
            replaceExpertRequests(key, result.expert_requests
              .filter((item) => item.team_id === teamId && typeof item.request_id === 'string')
              .map((item) => item.request_id));
            pendingKeys.delete(key);
          }
        }
      } catch {
        // Retry the authoritative query; never resend a previously accepted input.
      } finally {
        syncing = false;
      }
    };
    const unsubscribeState = webClient.onStateChange((state) => {
      if (state === 'ready') {
        void synchronize();
      } else {
        for (const key of expertProcessingSnapshot().keys) {
          pendingKeys.add(key);
          replaceExpertRequests(key, []);
        }
      }
    });
    const timer = window.setInterval(() => { void synchronize(); }, 10000);
    const lifecycleUnsubs = ['session.deleted', 'session.archived'].map((event) => webClient.on(event, ({ payload }) => {
      const prefix = `${payload.session_id}::`;
      for (const key of new Set([...pendingKeys, ...expertProcessingSnapshot().keys])) {
        if (key.startsWith(prefix)) {
          pendingKeys.delete(key);
          replaceExpertRequests(key, []);
        }
      }
    }));
    const events = [
      'chat.delta',
      'chat.reasoning',
      'chat.final',
      'chat.error',
      'chat.processing_status',
      'chat.interrupt_result',
      'org.turn_completed',
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
        if (event === 'chat.interrupt_result') {
          if (payload.success === true && Array.isArray(payload.cancelled_request_ids)) {
            for (const requestId of payload.cancelled_request_ids) {
              if (typeof requestId === 'string') finishExpertRequest(key, requestId);
            }
          }
          return;
        }
        if (event === 'org.turn_completed' || event === 'chat.error') {
          const roundId = String(payload.request_id ?? payload.rid ?? '');
          for (const [pendingKey, messageId] of streams) {
            if (pendingKey.startsWith(`${key}:`) && pendingKey.endsWith(`:${roundId}`)) {
              chat.updateMessage(key, messageId, { isStreaming: false });
              streams.delete(pendingKey);
            }
          }
          if (finishExpertRequest(key, roundId)) {
            chat.closeReasoning(key);
            chat.setThinking(key, false);
          }
          if (event === 'org.turn_completed') return;
        }
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
          const requestId = String(payload.request_id ?? payload.rid ?? '');
          if (payload.is_processing === true) {
            startExpertRequest(key, requestId);
          } else {
            for (const [pendingKey, messageId] of streams) {
              if (pendingKey.startsWith(`${key}:`) && pendingKey.endsWith(`:${requestId}`)) {
                chat.updateMessage(key, messageId, { isStreaming: false });
                streams.delete(pendingKey);
              }
            }
            if (finishExpertRequest(key, requestId)) {
              chat.closeReasoning(key);
              chat.setThinking(key, false);
            }
          }
        } else if (event === 'chat.error') {
          chat.addMessage(key, {
            id: generateUuidV4(),
            role: 'system',
            content: String(payload.error ?? ''),
            timestamp: new Date().toISOString(),
            teamId,
          });
        }
      }),
    );
    void synchronize();
    return () => {
      disposed = true;
      window.clearInterval(timer);
      unsubscribeState();
      [...unsubs, ...lifecycleUnsubs].forEach((unsubscribe) => unsubscribe());
      for (const key of expertProcessingSnapshot().keys) replaceExpertRequests(key, []);
    };
  }, [enabled]);
}
