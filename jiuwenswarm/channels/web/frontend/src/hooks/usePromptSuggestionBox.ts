import { useCallback, useEffect, useRef, useState } from 'react';
import { webClient, webRequest } from '../services/webClient';
import { useChatStore } from '../stores';
import type { ChatInputProvenance } from '../types';

// The user's likely next input after the answer the session just completed, as the gateway pushed it.
type Suggestion = { sessionId: string; requestId: string; suggestionId: string; text: string };

type CompletedPayload = { session_id?: unknown; request_id?: unknown };
type ReadyPayload = CompletedPayload & { suggestion_id?: unknown; text?: unknown };
type ProcessingPayload = CompletedPayload & { is_processing?: unknown };

function record(event_type: 'shown' | 'accepted' | 'ignored' | 'suppressed', suggestion: Suggestion, fields: object = {}) {
  void webRequest('prompt_suggestion.event', {
    event_type, session_id: suggestion.sessionId, suggestion_id: suggestion.suggestionId, ...fields,
  }).catch(() => {});
}

/**
 * One or two suggestions for the session's latest completed request. They show as chips after the
 * reply while the composer is empty and no answer is running. Clicking a chip sends that one; its
 * own close button drops only that chip. Tab fills the first into the composer. Leaving the page
 * or the conversation hides them; coming back shows the ones still open. Each counts as shown once
 * the page displays it, which the page reports through ``markShown``.
 */
export function usePromptSuggestionBox(activeSessionId: string | null, hasConversation: boolean, pageActive: boolean) {
  const [suggestions, setSuggestions] = useState<Suggestion[]>([]);
  const current = useRef<Suggestion[]>([]);
  // Which chips the page has already recorded as shown, and when that first happened.
  const shownAtById = useRef(new Map<string, number>());
  const shownIds = useRef(new Set<string>());
  // Closed, sent, or typed over. A later push of the same chip does not bring it back.
  const finished = useRef(new Set<string>());
  // The accepted suggestion while its session's composer still holds what it filled in.
  const accepted = useRef<Suggestion | null>(null);
  const unshown = useRef(new Map<string, Suggestion[]>());
  const completedRequests = useRef(new Map<string, string | null>());
  const enabled = useRef(false);
  // The session this page just sent in, until its request is running: a push meanwhile follows the
  // request before the send.
  const sending = useRef<string | null>(null);
  const cancelledDrafts = useRef(new Set<string>());
  const isProcessing = useChatStore(state => state.runtimes[activeSessionId ?? '']?.isProcessing ?? false);
  const composerEmpty = useChatStore(state => !(state.runtimes[activeSessionId ?? '']?.inputValue ?? '').trim());
  const visible = Boolean(
    suggestions.length && pageActive && hasConversation && suggestions[0].sessionId === activeSessionId && !isProcessing && composerEmpty,
  );
  const visibleRef = useRef(visible);
  visibleRef.current = visible;

  const replace = useCallback((next: Suggestion[]) => {
    current.current = next;
    setSuggestions(next);
  }, []);

  const finish = useCallback((items: Suggestion[]) => {
    for (const item of items) finished.current.add(item.suggestionId);
  }, []);

  // Closes every chip still open. Each that showed is recorded ignored for ``reason``.
  const close = useCallback((reason: 'dismissed' | 'sent_other' | 'typed' | null) => {
    const showing = current.current;
    if (reason) {
      for (const closing of showing) {
        if (shownIds.current.has(closing.suggestionId)) record('ignored', closing, { reason });
      }
    }
    finish(showing);
    if (showing[0]) unshown.current.delete(showing[0].sessionId);
    replace([]);
  }, [finish, replace]);

  const setEnabled = useCallback((next: boolean) => {
    enabled.current = next;
    if (next) return;
    unshown.current.clear();
    for (const sessionId of completedRequests.current.keys()) completedRequests.current.set(sessionId, null);
    close(null);
  }, [close]);

  useEffect(() => {
    const stopFinal = webClient.on<CompletedPayload>('chat.final', ({ payload }) => {
      const { session_id: sessionId, request_id: requestId } = payload;
      if (typeof sessionId !== 'string' || typeof requestId !== 'string') return;
      completedRequests.current.set(sessionId, requestId);
      if (unshown.current.get(sessionId)?.[0]?.requestId !== requestId) unshown.current.delete(sessionId);
      if (current.current[0]?.sessionId === sessionId && current.current[0]?.requestId !== requestId) close(null);
    });
    const stopReady = webClient.on<ReadyPayload>('prompt_suggestion.ready', ({ payload }) => {
      const { session_id: sessionId, request_id: requestId, suggestion_id: suggestionId, text } = payload;
      if (typeof sessionId !== 'string' || typeof requestId !== 'string' || typeof suggestionId !== 'string'
        || typeof text !== 'string' || !text.trim()) return;
      const state = useChatStore.getState();
      const completedRequest = completedRequests.current.get(sessionId);
      // The push can land while its turn is still winding down on the page, so a busy runtime holds
      // it for the visibility gate instead of dropping it. A request started since then has already
      // cleared the session's completed request, which makes this push stale.
      if (!enabled.current || sending.current === sessionId
        || (completedRequest !== undefined && completedRequest !== requestId)) return;
      if (finished.current.has(suggestionId)) return;
      const incoming = { sessionId, requestId, suggestionId, text: text.trim() };
      // It arrived after the user started typing in the session: src-raw's timing suppression.
      if (cancelledDrafts.current.has(sessionId) || state.runtimes[sessionId]?.inputValue.trim()) {
        record('suppressed', incoming, { reason: 'timing' });
        return;
      }
      const held = unshown.current.get(sessionId) ?? (
        current.current[0]?.sessionId === sessionId && current.current[0]?.requestId === requestId ? current.current : []
      );
      const sameRequest = held[0]?.requestId === requestId;
      if (sameRequest && (held.length >= 2 || held.some(item => item.suggestionId === suggestionId))) return;
      const next = sameRequest ? [...held, incoming] : [incoming];
      completedRequests.current.set(sessionId, requestId);
      unshown.current.set(sessionId, next);
      if (pageActive && state.activeSessionId === sessionId) replace(next);
    });
    // A new request in the session, from anywhere, ends the suggestion for the one before it. The
    // page also turns a session busy for late output of the request it just completed; that only
    // hides the suggestion until the page settles.
    const stopProcessing = webClient.on<ProcessingPayload>('chat.processing_status', ({ payload }) => {
      const { session_id: sessionId, request_id: requestId, is_processing: processing } = payload;
      if (processing !== true || typeof sessionId !== 'string' || typeof requestId !== 'string'
        || completedRequests.current.get(sessionId) === requestId) return;
      if (completedRequests.current.has(sessionId)) completedRequests.current.set(sessionId, null);
      unshown.current.delete(sessionId);
      if (current.current[0]?.sessionId === sessionId) close(null);
    });
    return () => { stopFinal(); stopReady(); stopProcessing(); };
  }, [replace, close, pageActive]);

  useEffect(() => {
    if (!enabled.current || !pageActive || !activeSessionId || !hasConversation || isProcessing || !composerEmpty
      || cancelledDrafts.current.has(activeSessionId) || sending.current === activeSessionId) return;
    const next = unshown.current.get(activeSessionId);
    if (next && completedRequests.current.get(activeSessionId) === next[0]?.requestId) replace(next);
  }, [activeSessionId, hasConversation, isProcessing, composerEmpty, pageActive, replace]);

  // The page displayed these chips. They stay with the session until the user closes them, sends,
  // or types, so leaving the page and coming back shows the same ones again.
  const markShown = useCallback(() => {
    const showing = current.current;
    if (!showing.length || !visibleRef.current) return;
    const now = Date.now();
    for (const item of showing) {
      if (shownIds.current.has(item.suggestionId)) continue;
      shownIds.current.add(item.suggestionId);
      shownAtById.current.set(item.suggestionId, now);
      record('shown', item);
    }
  }, []);

  // Emptying the composer drops the accepted suggestion: what is typed next is the user's own.
  useEffect(() => useChatStore.subscribe((state, previous) => {
    for (const sessionId of cancelledDrafts.current) {
      if (state.runtimes[sessionId]?.isProcessing) cancelledDrafts.current.delete(sessionId);
    }
    const taken = accepted.current;
    if (taken && !(state.runtimes[taken.sessionId]?.inputValue ?? '').trim()) accepted.current = null;
    if (sending.current && state.runtimes[sending.current]?.isProcessing) sending.current = null;
    const sessionId = state.activeSessionId;
    if (!sessionId || sessionId !== previous.activeSessionId) return;
    const runtime = state.runtimes[sessionId];
    const before = previous.runtimes[sessionId];
    if (!runtime?.inputValue.trim() || runtime.isProcessing || accepted.current?.sessionId === sessionId
      || sending.current === sessionId || cancelledDrafts.current.has(sessionId)) return;
    if (runtime.inputValue === before?.inputValue && !before?.isProcessing) return;
    cancelledDrafts.current.add(sessionId);
    close('typed');
    void webRequest('prompt_suggestion.event', { event_type: 'input_changed', session_id: sessionId }).catch(() => {});
  }), [close]);

  const takeOne = useCallback((method: 'click' | 'tab', suggestionId?: string) => {
    const showing = current.current;
    if (!showing.length || !visibleRef.current) return null;
    const taken = (suggestionId ? showing.find(item => item.suggestionId === suggestionId) : showing[0]) ?? null;
    if (!taken) return null;
    record('accepted', taken, {
      method,
      time_to_accept_ms: Math.max(0, Date.now() - (shownAtById.current.get(taken.suggestionId) ?? Date.now())),
    });
    for (const other of showing) {
      if (other.suggestionId !== taken.suggestionId && shownIds.current.has(other.suggestionId)) {
        record('ignored', other, { reason: 'sent_other' });
      }
    }
    finish(showing);
    unshown.current.delete(taken.sessionId);
    replace([]);
    accepted.current = taken;
    return taken;
  }, [finish, replace]);

  const accept = useCallback((method: 'click' | 'tab') => {
    const taken = takeOne(method);
    if (!taken) return false;
    useChatStore.getState().setInputValue(taken.sessionId, taken.text);
    window.dispatchEvent(new CustomEvent('chat-input-sync', { detail: { sessionId: taken.sessionId, value: taken.text } }));
    return true;
  }, [takeOne]);

  // The chip after the reply sends that suggestion as it stands: record the adoption, keep it as
  // the accepted one so the send attaches its provenance, and never put it in the composer.
  const acceptForSend = useCallback((suggestionId?: string): Suggestion | null => takeOne('click', suggestionId), [takeOne]);

  // Closes one chip. The other stays. Returns whether the row is now empty.
  const dismiss = useCallback((suggestionId?: string) => {
    const showing = current.current;
    if (!suggestionId) {
      const had = showing.length > 0;
      close('dismissed');
      return had;
    }
    const target = showing.find(item => item.suggestionId === suggestionId);
    if (!target) return showing.length === 0;
    if (shownIds.current.has(target.suggestionId)) record('ignored', target, { reason: 'dismissed' });
    finish([target]);
    const rest = showing.filter(item => item.suggestionId !== suggestionId);
    if (!rest.length) {
      unshown.current.delete(target.sessionId);
      replace([]);
      return true;
    }
    unshown.current.set(target.sessionId, rest);
    replace(rest);
    return false;
  }, [close, finish, replace]);

  // A send ends the suggestion. When the composer holds an accepted suggestion the message carries
  // its id and whether it was edited; a failed send restores the draft and, with it, that link.
  const takeSubmission = useCallback((content: string) => {
    close('sent_other');
    const { activeSessionId: sessionId, runtimes } = useChatStore.getState();
    if (sessionId && !runtimes[sessionId]?.isProcessing) sending.current = sessionId;
    const taken = accepted.current?.sessionId === sessionId ? accepted.current : null;
    if (taken) accepted.current = null;
    const provenance: ChatInputProvenance | undefined = taken ? {
      input_origin: 'prompt_suggestion',
      assistance_origin: 'prompt_suggestion',
      prompt_suggestion: { suggestion_id: taken.suggestionId, edited: content.trim() !== taken.text },
    } : undefined;
    const settle = (sent: boolean) => {
      if (sent) return;
      if (sending.current === sessionId) sending.current = null;
      const state = useChatStore.getState();
      if (taken && state.activeSessionId === taken.sessionId && state.runtimes[taken.sessionId]?.inputValue === content) {
        accepted.current = taken;
      }
    };
    return { provenance, settle };
  }, [close]);

  const visibleSuggestions = visible ? suggestions : [];
  return {
    text: visibleSuggestions[0]?.text ?? null,
    suggestions: visibleSuggestions,
    accept, acceptForSend, dismiss, takeSubmission, setEnabled, markShown,
  };
}

export type PromptSuggestionBox = ReturnType<typeof usePromptSuggestionBox>;
