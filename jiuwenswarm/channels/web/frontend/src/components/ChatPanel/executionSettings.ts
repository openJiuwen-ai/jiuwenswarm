// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

export interface ExecutionSettingsSnapshot {
  version: number;
  settings: {
    independent_git_identity: boolean;
    environment?: Record<string, string | null>;
  };
}

type SettingsRequest = (method: string, params: Record<string, unknown>) => Promise<ExecutionSettingsSnapshot>;

/** Preserve generic environment values and let the server reject stale updates. */
export async function saveIndependentGitIdentity(
  request: SettingsRequest,
  sessionId: string,
  enabled: boolean,
): Promise<ExecutionSettingsSnapshot> {
  const current = await request('session.execution_settings', { session_id: sessionId, mode: 'team' });
  return request('session.execution_settings', {
    session_id: sessionId,
    mode: 'team',
    expected_version: current.version,
    settings: { ...current.settings, independent_git_identity: enabled },
  });
}

/** An unpersisted read must not replace the first-message draft. */
export function restoredGitIdentity(snapshot: ExecutionSettingsSnapshot, draft: boolean): boolean {
  return snapshot.version >= 0 ? snapshot.settings.independent_git_identity : draft;
}
