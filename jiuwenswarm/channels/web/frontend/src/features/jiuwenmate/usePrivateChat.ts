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
  const historyRead = useRef<{
    api: JiuwenMateChatApi;
    scope: string;
    connectionEpoch: number;
    updatedRequestIds: Set<string>;
    promise: Promise<void>;
  } | null>(null);
  const unsent = useRef<{ scope: string; text: string; messageId: string } | null>(null);
  const listed = useRef(requests);
  const liveRequestIds = useRef(new Set<string>());
  const visibleRequests = loadedScope === scope ? requests : [];
  const visibleError = loadedScope === scope ? error : null;
  const loading = Boolean(agentId && conversationId && loadedScope !== scope);

  const reload = useCallback((): Promise<void> => {
    if (!agentId || !conversationId) return Promise.resolve();
    const pending = historyRead.current;
    if (pending?.api === api && pending.scope === scope && pending.connectionEpoch === connectionEpoch) {
      return pending.promise;
    }
    const read = { api, scope, connectionEpoch, updatedRequestIds: new Set<string>(), promise: Promise.resolve() };
    historyRead.current = read;
    read.promise = Promise.resolve().then(async () => {
      if (historyRead.current !== read) return;
      try {
        const history = await api.history(agentId, conversationId);
        if (historyRead.current !== read || scopeRef.current !== scope) return;
        setRequests((current) => {
          let reconciled = history;
          for (const request of current) {
            if (!read.updatedRequestIds.has(request.requestId)) continue;
            const snapshot = history.find((item) => item.requestId === request.requestId);
            // A non-paused ending cannot be resumed by a pending status read.
            if (snapshot?.status === 'ended' && snapshot.outcome !== 'paused'
              && (request.status !== 'ended' || request.outcome === 'paused')) continue;
            reconciled = mergeChatRequest(reconciled, request);
          }
          return reconciled;
        });
        setLoadedScope(scope);
        setError(null);
      } catch (reason) {
        if (historyRead.current === read && scopeRef.current === scope) {
          setError(errorMessage(reason));
          setLoadedScope(scope);
        }
      } finally {
        if (historyRead.current === read) historyRead.current = null;
      }
    });
    return read.promise;
  }, [api, agentId, conversationId, scope, connectionEpoch]);

  useEffect(() => {
    listed.current = visibleRequests;
  }, [visibleRequests]);

  useEffect(() => {
    if (scopeRef.current !== scope) {
      scopeRef.current = scope;
      setRequests([]);
      liveRequestIds.current.clear();
      setError(null);
    }
    if (!agentId || !conversationId) return;
    void reload();
    return () => {
      const pending = historyRead.current;
      if (pending?.api === api && pending.scope === scope && pending.connectionEpoch === connectionEpoch) {
        historyRead.current = null;
      }
    };
  }, [api, agentId, conversationId, scope, connectionEpoch, reload]);

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
            historyRead.current?.updatedRequestIds.add(request.requestId);
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
      liveRequestIds.current.add(event.requestId);
      historyRead.current?.updatedRequestIds.add(event.requestId);
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
      for (const result of results) {
        if (result.status === 'fulfilled') historyRead.current?.updatedRequestIds.add(result.value.requestId);
      }
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
        if (scopeRef.current === scope) {
          liveRequestIds.current.add(sent.request.requestId);
          historyRead.current?.updatedRequestIds.add(sent.request.requestId);
          setRequests((current) => mergeChatRequest(current, sent.request));
        }
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
        if (scopeRef.current === scope) {
          historyRead.current?.updatedRequestIds.add(controlled.requestId);
          setRequests((current) => mergeChatRequest(current, controlled));
        }
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
        if (scopeRef.current === scope) {
          historyRead.current?.updatedRequestIds.add(updated.requestId);
          setRequests((current) => mergeChatRequest(current, updated));
        }
      } catch (reason) {
        if (scopeRef.current === scope) setError(errorMessage(reason));
      }
    },
    [api, conversationId, scope],
  );

  return { liveRequestIds: liveRequestIds.current, requests: visibleRequests, error: visibleError, loading, sending, send, abort, decidePermission, reload };
}
