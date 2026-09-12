import { useSyncExternalStore } from 'react';

let enabled = false;
const listeners = new Set<() => void>();

export function setTaskFullDuplexEnabled(next: boolean): void {
  if (enabled === next) return;
  enabled = next;
  listeners.forEach((listener) => listener());
}

export function useTaskFullDuplexEnabled(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => enabled,
    () => false,
  );
}
