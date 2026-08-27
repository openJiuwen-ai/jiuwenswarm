// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useSyncExternalStore } from 'react';

let a2uiFeatureEnabled = true;
const a2uiFeatureListeners = new Set<() => void>();

/** Set the frontend A2UI feature flag from the server config payload. */
export function setA2UIFeatureEnabled(enabled: boolean): void {
  if (a2uiFeatureEnabled === enabled) {
    return;
  }
  a2uiFeatureEnabled = enabled;
  a2uiFeatureListeners.forEach((listener) => listener());
}

/** Return whether the frontend should parse and dispatch A2UI content. */
export function isA2UIFeatureEnabled(): boolean {
  return a2uiFeatureEnabled;
}

function subscribeA2UIFeatureEnabled(listener: () => void): () => void {
  a2uiFeatureListeners.add(listener);
  return () => {
    a2uiFeatureListeners.delete(listener);
  };
}

/** Subscribe React renderers to A2UI feature changes without reloading the page. */
export function useA2UIFeatureEnabled(): boolean {
  return useSyncExternalStore(subscribeA2UIFeatureEnabled, isA2UIFeatureEnabled, isA2UIFeatureEnabled);
}

/** Normalize config values sent over the WebSocket config RPC boundary. */
export function normalizeA2UIEnabled(value: unknown): boolean {
  if (typeof value === 'boolean') {
    return value;
  }
  const text = String(value ?? 'true')
    .trim()
    .toLowerCase();
  return !['0', 'false', 'no', 'off'].includes(text);
}
