// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

let a2uiFeatureEnabled = true;

/** Set the frontend A2UI feature flag from the server config payload. */
export function setA2UIFeatureEnabled(enabled: boolean) {
  a2uiFeatureEnabled = enabled;
}

/** Return whether the frontend should parse and dispatch A2UI content. */
export function isA2UIFeatureEnabled() {
  return a2uiFeatureEnabled;
}

/** Normalize config values sent over the WebSocket config RPC boundary. */
export function normalizeA2UIEnabled(value: unknown) {
  if (typeof value === 'boolean') {
    return value;
  }
  const text = String(value ?? 'true').trim().toLowerCase();
  return !['0', 'false', 'no', 'off'].includes(text);
}
