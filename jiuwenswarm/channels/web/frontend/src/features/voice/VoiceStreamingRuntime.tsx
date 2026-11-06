import { useCallback, useEffect, useRef } from 'react';
import { webClient } from '../../services/webClient';
import { useChatStore, useSessionStore } from '../../stores';
import { defaultGroupChatInput } from '../groupChatInput';
import { isTeamAgentMode } from '../planMode/wireMode';
import type { ChatSendOptions } from '../../types';
import { VoiceStreamingSession } from './streamingSession';
import { publishVoiceStreaming, registerVoiceStreaming } from './streamingStore';
import { getVoiceInputRoutingMode } from './routingMode';

export function VoiceStreamingRuntime({
  sessionId,
  send,
}: {
  sessionId: string | null;
  send: (text: string, options?: ChatSendOptions) => void | Promise<boolean | void>;
}) {
  const sender = useRef(send);
  sender.current = send;
  const current = useRef<{
    stream: VoiceStreamingSession;
    input: AbortController;
    failed: boolean;
    requests: Set<string>;
  } | null>(null);
  const stop = useCallback(() => {
    const active = current.current;
    if (!active) return;
    active.input.abort();
    active.stream.stop();
  }, []);
  const start = useCallback(
    async (id: string) => {
      if (useChatStore.getState().activeSessionId !== id) return;
      stop();
      if (webClient.getState() !== 'ready') throw new Error('disconnected');
      const input = new AbortController();
      const routingMode = getVoiceInputRoutingMode();
      const stream = new VoiceStreamingSession(webClient, id, {
        state: (state) => {
          if (current.current?.stream === stream) publishVoiceStreaming({ state });
        },
        transcript: async (text) => {
          if (
            input.signal.aborted ||
            current.current?.stream !== stream ||
            useChatStore.getState().activeSessionId !== id
          )
            return;
          publishVoiceStreaming({ transcript: text });
          const runtime = useSessionStore.getState().getRuntime(id);
          const groupChat = isTeamAgentMode(runtime?.mode)
            ? defaultGroupChatInput(id, runtime?.teamLeaderMemberIds[0] || 'team_leader')
            : undefined;
          const sent = await sender.current(text, {
            groupChat: groupChat
              ? { ...groupChat, mentions: routingMode === 'leader' ? groupChat.mentions : [] }
              : null,
            onRequestId: (requestId) => {
              const active = current.current;
              if (active?.stream !== stream) return;
              if (active.requests.size >= 64) active.requests.delete(active.requests.values().next().value!);
              active.requests.add(requestId);
            },
          });
          if (sent === false) throw new Error('deliveryFailed');
        },
        error: (error) => {
          if (current.current?.stream !== stream) return;
          current.current.failed = true;
          publishVoiceStreaming({ error });
        },
        ended: () => {
          if (current.current?.stream !== stream) return;
          const failed = current.current.failed;
          input.abort();
          current.current = null;
          publishVoiceStreaming({ state: failed ? 'failed' : 'idle' });
        },
      });
      current.current = { stream, input, failed: false, requests: new Set() };
      publishVoiceStreaming({ sessionId: id, state: 'starting', transcript: '', error: undefined });
      try {
        await stream.start();
      } catch (error) {
        if (input.signal.aborted) return;
        if (current.current?.stream === stream) {
          current.current.failed = true;
          publishVoiceStreaming({ error: error instanceof Error ? error.message : 'microphoneFailed' });
        }
        stream.stop();
      }
    },
    [stop],
  );

  useEffect(
    () => registerVoiceStreaming({ start, stop, pause: (paused) => current.current?.stream.setListening(!paused) }),
    [start, stop],
  );
  useEffect(() => {
    if (current.current && current.current.stream.sessionId !== sessionId) stop();
  }, [sessionId, stop]);
  useEffect(() => () => stop(), [stop]);
  useEffect(
    () =>
      webClient.on('chat.error', ({ payload }) => {
        const active = current.current;
        const requestId = payload.request_id;
        if (
          !active ||
          payload.session_id !== active.stream.sessionId ||
          typeof requestId !== 'string' ||
          !active.requests.has(requestId)
        )
          return;
        active.requests.delete(requestId);
        active.failed = true;
        publishVoiceStreaming({ error: String(payload.error || payload.message || 'deliveryFailed') });
      }),
    [],
  );
  return null;
}
