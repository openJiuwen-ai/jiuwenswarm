import type { Message } from '../types';

type LiveState = { sessionId: string; ready: boolean };

let state: LiveState = { sessionId: '', ready: false };
const listeners = new Set<() => void>();

export function subscribeTeamSessionLive(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function getTeamSessionLive(): LiveState {
  return state;
}

export function setTeamSessionLive(sessionId: string, ready: boolean): void {
  if (state.sessionId === sessionId && state.ready === ready) return;
  state = { sessionId, ready };
  listeners.forEach((listener) => listener());
}

export function isTeamSessionLive(sessionId: string): boolean {
  return state.ready && state.sessionId === sessionId;
}

/**
 * Enterprise follow-ups wait until the session live subscription matches.
 * Personal edition has no subscription, so an existing team session can still send.
 */
export function teamFollowUpLiveReady(
  enterprise: boolean,
  sessionId: string | null | undefined,
  live: LiveState,
  newConversationId = 'new',
): boolean {
  if (!enterprise || !sessionId || sessionId === newConversationId) return true;
  return live.ready && live.sessionId === sessionId;
}

/** 历史是打开瞬间的快照。订阅上已经画出、快照里没有的气泡留下来。 */
export function retainBubblesArrivedDuringHistory(
  restored: Message[],
  idsAtStart: ReadonlySet<string> | null,
  current: Message[],
  live: boolean,
): Message[] {
  if (!live || !idsAtStart) return restored;
  const restoredIds = new Set(restored.map((message) => message.id));
  const arrived = current.filter(
    (message) => !idsAtStart.has(message.id) && !restoredIds.has(message.id),
  );
  return arrived.length > 0 ? [...restored, ...arrived] : restored;
}
