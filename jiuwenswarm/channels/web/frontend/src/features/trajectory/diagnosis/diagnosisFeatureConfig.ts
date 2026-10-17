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

const DIAGNOSIS_VIEW_PREFIX = 'jiuwen.trajectory.diagnosisView.';

/** Read the trajectory sub-view last left for this session. */
export function readDiagnosisView(sessionId: string): boolean {
  if (sessionId === '' || sessionId === 'new') return false;
  try {
    return sessionStorage.getItem(`${DIAGNOSIS_VIEW_PREFIX}${sessionId}`) === 'diagnosis';
  } catch {
    return false;
  }
}

/** Remember which trajectory sub-view this session should reopen on. */
export function writeDiagnosisView(sessionId: string, diagnosisOpen: boolean): void {
  if (sessionId === '' || sessionId === 'new') return;
  try {
    sessionStorage.setItem(
      `${DIAGNOSIS_VIEW_PREFIX}${sessionId}`,
      diagnosisOpen ? 'diagnosis' : 'raw',
    );
  } catch {
    // Private mode or a full store. The current visit still keeps the choice in memory.
  }
}
