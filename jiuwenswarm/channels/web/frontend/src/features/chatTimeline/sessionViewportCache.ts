import type { VirtualItem } from '@tanstack/react-virtual';

export interface TimelineViewportState {
  measurements: VirtualItem[];
  offset: number;
}

export interface SessionScrollRestore {
  scrollTop: number;
  wasAtBottom: boolean;
}

interface SessionViewportState {
  scrollTop?: number;
  scrollAtBottom?: boolean;
  timeline?: TimelineViewportState;
}

const MAX_SESSION_VIEWPORTS = 20;
const sessionViewports = new Map<string, SessionViewportState>();

function updateSessionViewport(sessionId: string, update: Partial<SessionViewportState>): void {
  if (!sessionId) return;
  const current = sessionViewports.get(sessionId);
  sessionViewports.delete(sessionId);
  sessionViewports.set(sessionId, { ...current, ...update });
  while (sessionViewports.size > MAX_SESSION_VIEWPORTS) {
    const oldestKey = sessionViewports.keys().next().value;
    if (oldestKey === undefined) break;
    sessionViewports.delete(oldestKey);
  }
}

export function getSessionScrollTop(sessionId: string): number | undefined {
  return sessionViewports.get(sessionId)?.scrollTop;
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
  };
}

export function saveSessionScrollTop(sessionId: string, scrollTop: number, atBottom: boolean): void {
  updateSessionViewport(sessionId, { scrollTop, scrollAtBottom: atBottom });
}

export function getSessionTimelineViewport(sessionId: string): TimelineViewportState | undefined {
  return sessionViewports.get(sessionId)?.timeline;
}

export function saveSessionTimelineViewport(sessionId: string, timeline: TimelineViewportState): void {
  updateSessionViewport(sessionId, { timeline });
}

export function deleteSessionViewport(sessionId: string): void {
  sessionViewports.delete(sessionId);
}
