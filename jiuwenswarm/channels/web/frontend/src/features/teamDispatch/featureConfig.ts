// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useSyncExternalStore } from 'react';
import { parseConfigBoolean } from '../settings/services/settingsContract';

/** 团队任务调度模式，对应后端 TeamAgentSpec.dispatch_mode。 */
export type TeamDispatchMode = 'autonomous' | 'scheduled';

/**
 * 实验特性「调度模式」总开关（config.get 的 team_scheduled_dispatch_enabled）。
 * 打开后输入框“+”菜单才出现会话级调度模式开关；关闭时不随 chat.send 下发，
 * 后端沿用会话持久化值或团队配置。
 */
let enabled = false;
const listeners = new Set<() => void>();

export function setTeamScheduledDispatchEnabled(value: unknown): void {
  const next = parseConfigBoolean(value ?? false);
  if (next === enabled) return;
  enabled = next;
  listeners.forEach((listener) => listener());
}

export function isTeamScheduledDispatchEnabled(): boolean {
  return enabled;
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useTeamScheduledDispatchEnabled(): boolean {
  return useSyncExternalStore(subscribe, isTeamScheduledDispatchEnabled, isTeamScheduledDispatchEnabled);
}

/**
 * 会话实际生效的调度模式：用户在本会话拨过开关（或从会话 metadata 恢复）就用该值；
 * 否则默认自主模式，开关默认关闭。
 */
export function resolveTeamDispatchMode(sessionMode: TeamDispatchMode | undefined): TeamDispatchMode {
  return sessionMode ?? 'autonomous';
}

export function normalizeTeamDispatchMode(value: unknown): TeamDispatchMode | undefined {
  return value === 'autonomous' || value === 'scheduled' ? value : undefined;
}

/**
 * 欢迎页的调度模式在建会话时落成显式值，使首条 chat.send 带上 team_dispatch_mode 并由后端持久化。
 * 实验开关打开时团队配置为 scheduled，因此用户没拨过开关也要显式下发 autonomous，保证开关默认关闭；
 * 实验开关关闭且用户没拨过开关时返回 undefined：不下发，后端沿用团队配置。
 */
export function resolveNewSessionDispatchMode(
  pendingMode: TeamDispatchMode | undefined,
): TeamDispatchMode | undefined {
  if (pendingMode) return pendingMode;
  return isTeamScheduledDispatchEnabled() ? resolveTeamDispatchMode(undefined) : undefined;
}
