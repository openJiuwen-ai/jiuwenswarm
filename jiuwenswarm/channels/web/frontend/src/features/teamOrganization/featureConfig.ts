// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useSyncExternalStore } from 'react';
import { parseConfigBoolean } from '../settings/services/settingsContract';

let enabled = false;
const listeners = new Set<() => void>();

export function setTeamOrganizationUiEnabled(value: unknown): void {
  const next = parseConfigBoolean(value ?? false);
  if (next === enabled) return;
  enabled = next;
  listeners.forEach((listener) => listener());
}

export function isTeamOrganizationUiEnabled(): boolean {
  return enabled;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useTeamOrganizationUiEnabled(): boolean {
  return useSyncExternalStore(subscribe, isTeamOrganizationUiEnabled, isTeamOrganizationUiEnabled);
}
