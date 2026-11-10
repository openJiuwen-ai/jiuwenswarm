import { useSyncExternalStore } from 'react';

let enabled = false;
const listeners = new Set<() => void>();

export function setTeamMeetingEnabled(value: unknown): void {
  const next =
    value === true ||
    String(value ?? '')
      .trim()
      .toLowerCase() === 'true';
  if (enabled === next) return;
  enabled = next;
  listeners.forEach((listener) => listener());
}

export function useTeamMeetingEnabled(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => enabled,
    () => false,
  );
}
