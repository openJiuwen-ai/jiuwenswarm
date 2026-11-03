// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useSyncExternalStore } from 'react';
import { parseConfigBoolean } from '../settings/services/settingsContract';

let enabled = false;
let revision = 0;
const listeners = new Set<() => void>();

export function setMultiModelCollaborationEnabled(value: unknown): void {
  const next = parseConfigBoolean(value ?? false);
  if (next === enabled) return;
  enabled = next;
  revision += 1;
  listeners.forEach((listener) => listener());
}

export function getMultiModelCollaborationRevision(): number {
  return revision;
}

/** Apply a read only while no other writer has confirmed a different flag since it began. */
export function applyMultiModelCollaborationSnapshot(value: unknown, revisionAtRead: number): boolean {
  if (revisionAtRead !== revision) return false;
  setMultiModelCollaborationEnabled(value);
  return true;
}

export function isMultiModelCollaborationEnabled(): boolean {
  return enabled;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useMultiModelCollaborationEnabled(): boolean {
  return useSyncExternalStore(subscribe, isMultiModelCollaborationEnabled, isMultiModelCollaborationEnabled);
}
