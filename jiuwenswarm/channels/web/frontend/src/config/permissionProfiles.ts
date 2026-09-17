import type { AgentMode, Permission } from '../types';

const PERMISSION_OPTIONS: Permission[] = ['default', 'full_access'];

export function permissionOptionsForMode(_mode: AgentMode): Permission[] {
  return PERMISSION_OPTIONS;
}

export function effectivePermissionProfile(persistedProfile: Permission, _mode: AgentMode): Permission {
  if (persistedProfile === 'automatic') return 'default';
  return persistedProfile;
}
