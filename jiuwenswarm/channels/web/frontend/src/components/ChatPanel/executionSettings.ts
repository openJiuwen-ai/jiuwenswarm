// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

/**
 * Read the committed Git identity switch from session metadata.
 *
 * The backend mirrors the team-session settings it committed on chat.send
 * into ``metadata.execution_settings``. Returns undefined when nothing was
 * committed yet, so an unsent draft is not overwritten.
 */
export function restoredGitIdentity(metadata: Record<string, unknown> | null | undefined): boolean | undefined {
  const settings = metadata?.execution_settings;
  if (!settings || typeof settings !== 'object') return undefined;
  const enabled = (settings as { independent_git_identity?: unknown }).independent_git_identity;
  return typeof enabled === 'boolean' ? enabled : undefined;
}
