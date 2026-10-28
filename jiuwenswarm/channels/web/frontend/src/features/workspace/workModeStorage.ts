import type { WorkMode } from './projectTypes';

const WORK_MODE_STORAGE_KEY = 'jiuwenswarm_work_mode';
const DEFAULT_WORK_MODE: WorkMode = 'work';

export function readStoredWorkMode(): WorkMode {
  if (typeof window === 'undefined') return DEFAULT_WORK_MODE;
  try {
    const stored = window.localStorage.getItem(WORK_MODE_STORAGE_KEY);
    return stored === 'code' || stored === 'design' ? stored : DEFAULT_WORK_MODE;
  } catch {
    return DEFAULT_WORK_MODE;
  }
}

export function persistWorkMode(workMode: WorkMode): void {
  if (typeof window === 'undefined') return;
  try {
    window.localStorage.setItem(WORK_MODE_STORAGE_KEY, workMode);
  } catch {
    // Storage can be unavailable in private, restricted, or embedded contexts.
  }
}
