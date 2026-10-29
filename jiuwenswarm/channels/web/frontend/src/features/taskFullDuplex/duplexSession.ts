import { useSyncExternalStore } from 'react';

// Session the voice duplex run is bound to; its main chat cannot use free models (#5855).
let duplexSessionId: string | null = null;
const listeners = new Set<() => void>();

export function setTaskFullDuplexSessionId(next: string | null): void {
  if (duplexSessionId === next) return;
  duplexSessionId = next;
  listeners.forEach((listener) => listener());
}

export function useTaskFullDuplexSessionId(): string | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => duplexSessionId,
    () => null,
  );
}
