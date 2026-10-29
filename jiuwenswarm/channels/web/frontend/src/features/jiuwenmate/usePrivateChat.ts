import { useCallback, useEffect, useRef, useState } from 'react';
import {
  applyStreamEvent,
  errorMessage,
  isPending,
  mergeChatRequest,
  type JiuwenMateApi,
  type JiuwenMateChatRequest,
  type JiuwenMateControlReason,
  type JiuwenMateConversation,
} from './jiuwenmateApi';

export type JiuwenMateChatApi = Pick<
  JiuwenMateApi,
  | 'sendMessage'
  | 'history'
  | 'getRequest'
  | 'abortRequest'
  | 'decidePermission'
  | 'onStream'
  | 'listConversations'
  | 'renameConversation'
  | 'setConversationPinned'
  | 'deleteConversation'
> & Partial<Pick<JiuwenMateApi, 'sharing'>>;

/** Messages run on the server; pushed fragments show progress, and asking again covers missed pushes. */
const POLL_INTERVAL_MS = 1500;

export function newId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`;
}

/**
 * One Web private conversation with a colleague. History is read again when the colleague or
 * conversation changes or the connection comes back; a message that failed to send keeps its message id,
 * so sending it again cannot create a second request. Pushed fragments update the running
 * message's draft; its end, or a fragment of a message not listed yet, is followed by a query.
 */
export function usePrivateChat(api: JiuwenMateChatApi, agentId: string, conversationId: string, connectionEpoch: number) {
  const scope = `${agentId} ${conversationId}`;
  const [requests, setRequests] = useState<JiuwenMateChatRequest[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loadedScope, setLoadedScope] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [pollRound, setPollRound] = useState(0);
  const scopeRef = useRef(scope);
  const reloadSeq = useRef(0);
  const unsent = useRef<{ scope: string; text: string; messageId: string } | null>(null);
  const listed = useRef(requests);
  const visibleRequests = loadedScope === scope ? requests : [];
  const visibleError = loadedScope === scope ? error : null;
  const loading = Boolean(agentId && conversationId && loadedScope !== scope);

  useEffect(() => {
    listed.current = visibleRequests;
  }, [visibleRequests]);

  useEffect(() => {
    if (scopeRef.current !== scope) {
      scopeRef.current = scope;
      setRequests([]);
      setError(null);
    }
    if (!agentId || !conversationId) return;
    let active = true;
    api.history(agentId, conversationId).then(
      (listed) => {
        if (active) {
          setRequests(listed);
          setLoadedScope(scope);
        }
      },
      (reason: unknown) => {
        if (active) {
          setError(errorMessage(reason));
          setLoadedScope(scope);
        }
      },
    );
    return () => {
      active = false;
    };
  }, [api, agentId, conversationId, scope, connectionEpoch]);

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
          if (active && request.conversationId === conversationId) {
            setRequests((current) => mergeChatRequest(current, request));
          }
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
  }, [api, agentId, conversationId]);

  const pendingIds = visibleRequests
    .filter(
      (item) =>
        isPending(item) ||
        (item.status === 'ended' &&
          item.outcome === 'paused' &&
          item.approval !== null &&
          ['approved', 'rejected', 'expired'].includes(item.approval.status)),
    )
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

  /** Resolves with the conversation as now listed, or null when sending failed. */
  const send = useCallback(
    async (text: string): Promise<JiuwenMateConversation | null> => {
      const previous = unsent.current;
      const messageId = previous && previous.scope === scope && previous.text === text ? previous.messageId : newId();
      unsent.current = { scope, text, messageId };
      setSending(true);
      setError(null);
      try {
        const sent = await api.sendMessage(agentId, conversationId, text, messageId);
        unsent.current = null;
        if (scopeRef.current === scope) setRequests((current) => mergeChatRequest(current, sent.request));
        return sent.conversation;
      } catch (reason) {
        if (scopeRef.current === scope) setError(errorMessage(reason));
        return null;
      } finally {
        setSending(false);
      }
    },
    [api, agentId, conversationId, scope],
  );

  const abort = useCallback(
    async (requestId: string, reason: JiuwenMateControlReason): Promise<void> => {
      setError(null);
      try {
        const controlled = await api.abortRequest(requestId, reason);
        if (scopeRef.current === scope) setRequests((current) => mergeChatRequest(current, controlled));
      } catch (reason) {
        if (scopeRef.current === scope) setError(errorMessage(reason));
      }
    },
    [api, scope],
  );

  const decidePermission = useCallback(
    async (approvalId: string, decision: 'approve' | 'reject', toolCallId?: string): Promise<void> => {
      setError(null);
      try {
        const updated = await api.decidePermission(approvalId, conversationId, decision, toolCallId);
        if (scopeRef.current === scope) setRequests((current) => mergeChatRequest(current, updated));
      } catch (reason) {
        if (scopeRef.current === scope) setError(errorMessage(reason));
      }
    },
    [api, conversationId, scope],
  );

  const reload = useCallback(async () => {
    if (!agentId || !conversationId) return;
    const requested = scope;
    const seq = ++reloadSeq.current;
    try {
      const listed = await api.history(agentId, conversationId);
      if (scopeRef.current !== requested || seq !== reloadSeq.current) return;
      setRequests(listed);
      setLoadedScope(requested);
      setError(null);
    } catch (reason) {
      if (scopeRef.current === requested && seq === reloadSeq.current) setError(errorMessage(reason));
    }
  }, [api, agentId, conversationId, scope]);

  return { requests: visibleRequests, error: visibleError, loading, sending, send, abort, decidePermission, reload };
}
