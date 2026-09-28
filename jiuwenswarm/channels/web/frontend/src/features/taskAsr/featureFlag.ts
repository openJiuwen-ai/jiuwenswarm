import { useSyncExternalStore } from 'react';

let enabled = false;
const listeners = new Set<() => void>();

export function setTaskAsrEnabled(next: boolean): void {
  if (enabled === next) return;
  enabled = next;
  listeners.forEach((listener) => listener());
}

export function useTaskAsrEnabled(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => enabled,
    () => true,
  );
}
