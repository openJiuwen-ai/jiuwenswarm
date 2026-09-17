import type { WsEvent } from '../types';

type EventDispatcher = (event: WsEvent) => void;

interface SuspendedSessionEvents {
  depth: number;
  events: WsEvent[];
  flushScheduled: boolean;
}

const IMMEDIATE_SESSION_EVENTS = new Set(['history.message', 'chat.error', 'security.alert']);

function normalizeSessionId(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined;
  return value.trim() || undefined;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

export function getEventSessionId(event: WsEvent): string | undefined {
  const payload = event.payload;
  const direct = normalizeSessionId(payload.session_id);
  if (direct) return direct;

  const nestedPayload = payload.payload;
  if (isRecord(nestedPayload)) {
    const nested = normalizeSessionId(nestedPayload.session_id);
    if (nested) return nested;

    const nestedEvent = nestedPayload.event;
    if (isRecord(nestedEvent)) {
      const nestedEventSessionId = normalizeSessionId(nestedEvent.session_id);
      if (nestedEventSessionId) return nestedEventSessionId;
    }
  }

  const nestedEvent = payload.event;
  return isRecord(nestedEvent) ? normalizeSessionId(nestedEvent.session_id) : undefined;
}

export interface SessionEventGate {
  dispatch(event: WsEvent): void;
  suspend(sessionId: string): () => void;
}

export function createSessionEventGate(dispatchEvent: EventDispatcher): SessionEventGate {
  const suspendedSessions = new Map<string, SuspendedSessionEvents>();

  function flush(sessionId: string, suspended: SuspendedSessionEvents): void {
    suspended.flushScheduled = false;
    if (suspended.depth > 0 || suspendedSessions.get(sessionId) !== suspended) {
      return;
    }

    suspendedSessions.delete(sessionId);
    for (const event of suspended.events) {
      dispatchEvent(event);
    }
  }

  function scheduleFlush(sessionId: string, suspended: SuspendedSessionEvents): void {
    if (suspended.flushScheduled) return;
    suspended.flushScheduled = true;
    queueMicrotask(() => flush(sessionId, suspended));
  }

  return {
    dispatch(event): void {
      const sessionId = getEventSessionId(event);
      const suspended = sessionId ? suspendedSessions.get(sessionId) : undefined;
      if (!suspended || IMMEDIATE_SESSION_EVENTS.has(event.event)) {
        dispatchEvent(event);
        return;
      }
      suspended.events.push(event);
    },

    suspend(sessionId): () => void {
      const normalizedSessionId = normalizeSessionId(sessionId);
      if (!normalizedSessionId) {
        throw new Error('sessionId is required to suspend session events');
      }

      const suspended = suspendedSessions.get(normalizedSessionId) ?? {
        depth: 0,
        events: [],
        flushScheduled: false,
      };
      suspended.depth += 1;
      suspendedSessions.set(normalizedSessionId, suspended);

      let released = false;
      return function release(): void {
        if (released) return;
        released = true;
        suspended.depth -= 1;
        if (suspended.depth === 0) {
          scheduleFlush(normalizedSessionId, suspended);
        }
      };
    },
  };
}
