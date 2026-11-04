import type { AgentMode, Permission } from '../types';

const NON_AGENT_PERMISSION_OPTIONS: Permission[] = ['default', 'full_access'];
const AGENT_PERMISSION_OPTIONS: Permission[] = ['default', 'automatic', 'full_access'];

export function permissionOptionsForMode(mode: AgentMode): Permission[] {
  return mode === 'agent' ? AGENT_PERMISSION_OPTIONS : NON_AGENT_PERMISSION_OPTIONS;
}

export function effectivePermissionProfile(persistedProfile: Permission, mode: AgentMode): Permission {
  if (persistedProfile === 'automatic' && mode !== 'agent') return 'default';
  return persistedProfile;
}
