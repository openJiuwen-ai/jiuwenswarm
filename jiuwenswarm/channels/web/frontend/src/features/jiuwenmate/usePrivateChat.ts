import { useCallback, useEffect, useRef, useState } from 'react';
import {
  applyStreamEvent,
  errorMessage,
  isPending,
  mergeChatRequest,
  type JiuwenMateApi,
  type JiuwenMateChatRequest,
} from './jiuwenmateApi';

export type JiuwenMateChatApi = Pick<JiuwenMateApi, 'sendMessage' | 'history' | 'getRequest' | 'onStream'>;

/** Messages run on the server; pushed fragments show progress, and asking again covers missed pushes. */
const POLL_INTERVAL_MS = 1500;

function newMessageId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/**
 * The Web private conversation with one colleague. History is read again when the colleague
 * changes or the connection comes back; a message that failed to send keeps its message id,
 * so sending it again cannot create a second request. Pushed fragments update the running
 * message's draft; its end, or a fragment of a message not listed yet, is followed by a query.
 */
export function usePrivateChat(api: JiuwenMateChatApi, agentId: string, connectionEpoch: number) {
  const [requests, setRequests] = useState<JiuwenMateChatRequest[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [pollRound, setPollRound] = useState(0);
  const agentRef = useRef(agentId);
  const unsent = useRef<{ agentId: string; text: string; messageId: string } | null>(null);
  const listed = useRef(requests);

  useEffect(() => {
    listed.current = requests;
  }, [requests]);

  useEffect(() => {
    if (agentRef.current !== agentId) {
      agentRef.current = agentId;
      setRequests([]);
      setError(null);
    }
    if (!agentId) return;
    let active = true;
    api.history(agentId).then(
      (listed) => {
        if (active) setRequests(listed);
      },
      (reason: unknown) => {
        if (active) setError(errorMessage(reason));
      },
    );
    return () => {
      active = false;
    };
  }, [api, agentId, connectionEpoch]);

  useEffect(() => {
    if (!agentId) return;
    let active = true;
    const fetching = new Set<string>();
    const refresh = (requestId: string) => {
      if (fetching.has(requestId)) return;
      fetching.add(requestId);
      api.getRequest(requestId).then(
        (request) => {
          fetching.delete(requestId);
          if (active) setRequests((current) => mergeChatRequest(current, request));
        },
        () => {
          fetching.delete(requestId);
        },
      );
    };
    const unsubscribe = api.onStream((event) => {
      if (!active || event.agentId !== agentId) return;
      const known = listed.current.some((item) => item.requestId === event.requestId);
      setRequests((current) => applyStreamEvent(current, event) ?? current);
      if (!known || event.kind === 'ended') refresh(event.requestId);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, [api, agentId]);

  const pendingIds = requests
    .filter(isPending)
    .map((item) => item.requestId)
    .join(' ');

  useEffect(() => {
    if (!pendingIds) return;
    let active = true;
    const timer = window.setTimeout(async () => {
      const results = await Promise.allSettled(pendingIds.split(' ').map((id) => api.getRequest(id)));
      if (!active) return;
      setRequests((current) =>
        results.reduce(
          (list, result) => (result.status === 'fulfilled' ? mergeChatRequest(list, result.value) : list),
          current,
        ),
      );
      setPollRound((round) => round + 1);
    }, POLL_INTERVAL_MS);
    return () => {
      active = false;
      window.clearTimeout(timer);
    };
  }, [api, agentId, pendingIds, pollRound]);

  const send = useCallback(
    async (text: string): Promise<boolean> => {
      const previous = unsent.current;
      const messageId =
        previous && previous.agentId === agentId && previous.text === text ? previous.messageId : newMessageId();
      unsent.current = { agentId, text, messageId };
      setSending(true);
      setError(null);
      try {
        const request = await api.sendMessage(agentId, text, messageId);
        unsent.current = null;
        if (agentRef.current === agentId) setRequests((current) => mergeChatRequest(current, request));
        return true;
      } catch (reason) {
        if (agentRef.current === agentId) setError(errorMessage(reason));
        return false;
      } finally {
        setSending(false);
      }
    },
    [api, agentId],
  );

  return { requests, error, sending, send };
}
