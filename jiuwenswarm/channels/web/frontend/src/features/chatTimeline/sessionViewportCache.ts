import type { VirtualItem } from '@tanstack/react-virtual';

export interface TimelineViewportState {
  measurements: VirtualItem[];
  offset: number;
  anchorKey?: string;
  anchorOffset?: number;
  atBottom?: boolean;
}

export interface SessionScrollRestore {
  scrollTop: number;
  wasAtBottom: boolean;
  hasTimelineAnchor: boolean;
}

interface SessionViewportState {
  scrollTop?: number;
  scrollAtBottom?: boolean;
  timeline?: TimelineViewportState;
}

interface SessionViewportGeneration {
  valid: boolean;
  writerCount: number;
}

export interface SessionViewportToken {
  readonly sessionId: string;
}

interface SessionViewportTokenState {
  generation: SessionViewportGeneration;
  released: boolean;
}

const MAX_SESSION_VIEWPORTS = 20;
const SCROLL_BOTTOM_THRESHOLD_PX = 40;
const sessionViewports = new Map<string, SessionViewportState>();
const sessionGenerations = new Map<string, SessionViewportGeneration>();
const tokenStates = new WeakMap<SessionViewportToken, SessionViewportTokenState>();

export function isScrollAtBottom(el: HTMLElement): boolean {
  return el.scrollHeight - el.scrollTop - el.clientHeight < SCROLL_BOTTOM_THRESHOLD_PX;
}

export function acquireSessionViewportToken(sessionId: string): SessionViewportToken | null {
  if (!sessionId) return null;
  let generation = sessionGenerations.get(sessionId);
  if (!generation) {
    generation = { valid: true, writerCount: 0 };
    sessionGenerations.set(sessionId, generation);
  }
  generation.writerCount += 1;
  const token = { sessionId };
  tokenStates.set(token, { generation, released: false });
  return token;
}

export function releaseSessionViewportToken(token: SessionViewportToken | null): void {
  if (!token) return;
  const state = tokenStates.get(token);
  if (!state || state.released) return;
  state.released = true;
  const { generation } = state;
  generation.writerCount -= 1;
  if (generation.writerCount === 0) {
    generation.valid = false;
    if (sessionGenerations.get(token.sessionId) === generation) {
      sessionGenerations.delete(token.sessionId);
    }
  }
  tokenStates.delete(token);
}

function updateSessionViewport(token: SessionViewportToken | null, update: Partial<SessionViewportState>): void {
  const state = token ? tokenStates.get(token) : undefined;
  if (!token || !state || state.released || !state.generation.valid) return;
  const { sessionId } = token;
  const current = sessionViewports.get(sessionId);
  sessionViewports.delete(sessionId);
  sessionViewports.set(sessionId, { ...current, ...update });
  while (sessionViewports.size > MAX_SESSION_VIEWPORTS) {
    const oldestKey = sessionViewports.keys().next().value;
    if (oldestKey === undefined) break;
    sessionViewports.delete(oldestKey);
  }
}

export function getSessionScrollRestore(
  sessionId: string,
  scrollHeight: number,
  clientHeight: number,
): SessionScrollRestore | undefined {
  const viewport = sessionViewports.get(sessionId);
  if (viewport?.scrollTop === undefined || viewport.scrollAtBottom === undefined) return undefined;
  return {
    scrollTop: viewport.scrollAtBottom ? Math.max(0, scrollHeight - clientHeight) : viewport.scrollTop,
    wasAtBottom: viewport.scrollAtBottom,
    hasTimelineAnchor: viewport.timeline?.anchorKey !== undefined,
  };
}

export function saveSessionScrollTop(
  token: SessionViewportToken | null,
  scrollTop: number,
  atBottom: boolean,
): void {
  updateSessionViewport(token, { scrollTop, scrollAtBottom: atBottom });
}

export function getSessionTimelineViewport(sessionId: string): TimelineViewportState | undefined {
  return sessionViewports.get(sessionId)?.timeline;
}

export function saveSessionTimelineViewport(
  token: SessionViewportToken | null,
  timeline: TimelineViewportState,
): void {
  updateSessionViewport(token, { timeline });
}

export function deleteSessionViewport(sessionId: string): void {
  sessionViewports.delete(sessionId);
  const generation = sessionGenerations.get(sessionId);
  if (generation) generation.valid = false;
}
