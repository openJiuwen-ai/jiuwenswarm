import { useSyncExternalStore } from 'react';

let diagnosisEnabled = false;
const listeners = new Set<() => void>();

/** Normalize the diagnosis switch from the config RPC boundary. */
export function normalizeTrajectoryDiagnosisEnabled(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  return ['1', 'true', 'yes', 'on', 'enabled'].includes(
    String(value ?? '').trim().toLowerCase(),
  );
}

/** Publish the latest persisted diagnosis setting to the current window. */
export function setTrajectoryDiagnosisEnabled(enabled: boolean): void {
  if (diagnosisEnabled === enabled) return;
  diagnosisEnabled = enabled;
  listeners.forEach(listener => listener());
}

/** Return the current diagnosis feature state. */
export function isTrajectoryDiagnosisEnabled(): boolean {
  return diagnosisEnabled;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Subscribe trajectory surfaces to diagnosis setting changes without reloading. */
export function useTrajectoryDiagnosisEnabled(): boolean {
  return useSyncExternalStore(subscribe, isTrajectoryDiagnosisEnabled, isTrajectoryDiagnosisEnabled);
}
