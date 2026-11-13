import { useSyncExternalStore } from 'react';

let reaFeatureEnabled = true;
const reaFeatureListeners = new Set<() => void>();

export function setREAFeatureEnabled(enabled: boolean): void {
  if (reaFeatureEnabled === enabled) return;
  reaFeatureEnabled = enabled;
  reaFeatureListeners.forEach((listener) => listener());
}

export function isREAFeatureEnabled(): boolean {
  return reaFeatureEnabled;
}

function subscribeREAFeatureEnabled(listener: () => void): () => void {
  reaFeatureListeners.add(listener);
  return () => {
    reaFeatureListeners.delete(listener);
  };
}

export function useREAFeatureEnabled(): boolean {
  return useSyncExternalStore(
    subscribeREAFeatureEnabled,
    isREAFeatureEnabled,
    isREAFeatureEnabled,
  );
}

export function normalizeREAEnabled(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  const text = String(value ?? 'true')
    .trim()
    .toLowerCase();
  return !['0', 'false', 'no', 'off'].includes(text);
}
