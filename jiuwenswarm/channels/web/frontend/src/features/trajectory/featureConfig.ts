// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useSyncExternalStore } from 'react';

let trajectoryUiEnabled = false;
const listeners = new Set<() => void>();

/** Normalize the trajectory switch from the config RPC boundary. */
export function normalizeTrajectoryUiEnabled(value: unknown): boolean {
  if (typeof value === 'boolean') return value;
  return ['1', 'true', 'yes', 'on', 'enabled'].includes(
    String(value ?? '').trim().toLowerCase(),
  );
}

/** Publish the latest persisted trajectory UI setting to the current window. */
export function setTrajectoryUiEnabled(enabled: boolean): void {
  if (trajectoryUiEnabled === enabled) return;
  trajectoryUiEnabled = enabled;
  listeners.forEach(listener => listener());
}

/** Return the current trajectory UI feature state. */
export function isTrajectoryUiEnabled(): boolean {
  return trajectoryUiEnabled;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** Subscribe App surfaces to trajectory setting changes without reloading. */
export function useTrajectoryUiEnabled(): boolean {
  return useSyncExternalStore(subscribe, isTrajectoryUiEnabled, isTrajectoryUiEnabled);
}
