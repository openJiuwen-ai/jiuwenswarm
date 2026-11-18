// A design URL does not restore session.model_selection by itself. Filling the
// Settings default name before that read picks the first catalogue row that
// shares the name, and the canvas then saves that id onto the graph.

export type DesignModelRestoreStatus = 'loading' | 'ready' | 'failed';

export type DesignModelRestoreGate = {
  sessionId: string;
  status: DesignModelRestoreStatus;
  attempt: number;
};

type Listener = () => void;

let gate: DesignModelRestoreGate | null = null;
const listeners = new Set<Listener>();

export function designSessionPendingRestore(
  routeKind: string,
  routeSessionId: string | null | undefined,
  newConversationId: string,
): string | null {
  if (routeKind !== 'design-project') return null;
  const sessionId = String(routeSessionId ?? '').trim();
  if (!sessionId || sessionId === newConversationId || sessionId === 'new') return null;
  return sessionId;
}

export function alignDesignModelGate(
  current: DesignModelRestoreGate | null,
  sessionId: string | null,
): DesignModelRestoreGate | null {
  if (!sessionId) return null;
  if (current?.sessionId === sessionId) return current;
  return { sessionId, status: 'loading', attempt: 0 };
}

export function shouldHoldDesignModelDefault(
  current: DesignModelRestoreGate | null,
  sessionId: string,
): boolean {
  return Boolean(current && current.sessionId === sessionId && current.status !== 'ready');
}

export function getDesignModelGate(): DesignModelRestoreGate | null {
  return gate;
}

export function subscribeDesignModelGate(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

export function publishDesignModelGate(next: DesignModelRestoreGate | null): void {
  if (next === gate) return;
  if (
    next
    && gate
    && next.sessionId === gate.sessionId
    && next.status === gate.status
    && next.attempt === gate.attempt
  ) {
    return;
  }
  gate = next;
  listeners.forEach((listener) => listener());
}

export function requestDesignModelRestoreRetry(): void {
  if (!gate || gate.status !== 'failed') return;
  publishDesignModelGate({
    sessionId: gate.sessionId,
    status: 'loading',
    attempt: gate.attempt + 1,
  });
}
