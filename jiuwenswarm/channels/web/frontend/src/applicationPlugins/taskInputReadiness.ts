import { useSyncExternalStore } from 'react';

const pending = new Map<symbol, string | null>();
const listeners = new Set<() => void>();
const notify = () => listeners.forEach((listener) => listener());

/** A null session covers conversation creation, before its ID is known. */
export function blockTaskInput(sessionId: string | null): () => void {
  const token = Symbol();
  pending.set(token, sessionId);
  notify();
  return () => {
    if (pending.delete(token)) notify();
  };
}

export function isTaskInputBlocked(sessionId: string | null): boolean {
  return [...pending.values()].some((target) => target === null || target === sessionId);
}

export function useTaskInputBlocked(sessionId: string | null): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => isTaskInputBlocked(sessionId),
    () => false,
  );
}
