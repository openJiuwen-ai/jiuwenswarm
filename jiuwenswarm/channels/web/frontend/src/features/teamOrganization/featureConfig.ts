// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useSyncExternalStore } from 'react';
import { isEnterprise } from '../../edition';
import { parseConfigBoolean } from '../settings/services/settingsContract';

// 企业版设置页被藏起，用户无法自己打开这个开关。先按企业版默认打开，
// config.get 返回 false 时仍会关掉。个人版保持关闭。
let enabled = typeof window !== 'undefined' && isEnterprise();
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
