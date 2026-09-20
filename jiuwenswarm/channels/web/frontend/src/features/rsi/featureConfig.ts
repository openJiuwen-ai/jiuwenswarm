import { useSyncExternalStore } from 'react';

let rsiFeatureEnabled = true;
const rsiFeatureListeners = new Set<() => void>();

export function setRSIFeatureEnabled(enabled: boolean): void {
  if (rsiFeatureEnabled === enabled) return;
  rsiFeatureEnabled = enabled;
  rsiFeatureListeners.forEach((listener) => listener());
}

export function isRSIFeatureEnabled(): boolean {
  return rsiFeatureEnabled;
}

function subscribeRSIFeatureEnabled(listener: () => void): () => void {
  rsiFeatureListeners.add(listener);
  return () => {
    rsiFeatureListeners.delete(listener);
  };
}

export function useRSIFeatureEnabled(): boolean {
  return useSyncExternalStore(subscribeRSIFeatureEnabled, isRSIFeatureEnabled, isRSIFeatureEnabled);
}

export function normalizeRSIEnabled(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  const text = String(value ?? 'true')
    .trim()
    .toLowerCase();
  return !['0', 'false', 'no', 'off'].includes(text);
}
