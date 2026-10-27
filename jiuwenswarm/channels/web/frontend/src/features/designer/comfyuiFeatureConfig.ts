import { useSyncExternalStore } from 'react';
import { parseConfigBoolean } from '../settings/services/settingsContract';

let enabled = false;
const listeners = new Set<() => void>();

export function setComfyuiWorkflowEnabled(value: unknown): void {
  const next = parseConfigBoolean(value ?? false);
  if (next === enabled) return;
  enabled = next;
  listeners.forEach((listener) => listener());
}

export function isComfyuiWorkflowEnabled(): boolean {
  return enabled;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useComfyuiWorkflowEnabled(): boolean {
  return useSyncExternalStore(subscribe, isComfyuiWorkflowEnabled, isComfyuiWorkflowEnabled);
}
