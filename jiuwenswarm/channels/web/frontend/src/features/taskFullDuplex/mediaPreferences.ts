import { useSyncExternalStore } from 'react';

const STORAGE_KEY = 'jiuwen.taskFullDuplex.screenSharing';
const listeners = new Set<() => void>();

export function isTaskDuplexScreenSharingEnabled(): boolean {
  try {
    return window.localStorage.getItem(STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

export function setTaskDuplexScreenSharingEnabled(enabled: boolean): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, String(enabled));
  } catch {
    // Browser storage can be disabled; sharing remains opt-in for each start.
  }
  listeners.forEach((listener) => listener());
}

export function useTaskDuplexScreenSharingEnabled(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      window.addEventListener('storage', listener);
      return () => {
        listeners.delete(listener);
        window.removeEventListener('storage', listener);
      };
    },
    isTaskDuplexScreenSharingEnabled,
    () => false,
  );
}
