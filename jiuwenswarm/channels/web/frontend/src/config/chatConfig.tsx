import { CircleAlert } from 'lucide-react';
import type { AgentMode, Permission } from '../types';

export interface ChatOptionDef<T extends string> {
  value: T;
  i18nKey: string;
  descriptionI18nKey?: string;
  icon: (props: { className?: string }) => JSX.Element;
}

// ── 工作模式图标 ────────────────────────────────────────────────

function ClusterModeIcon({ className }: { className?: string }) {
  return <span className={`chat-config-icon chat-config-icon--cluster ${className ?? ''}`} aria-hidden="true" />;
}

function SingleAgentModeIcon({ className }: { className?: string }) {
  return <span className={`chat-config-icon chat-config-icon--single-agent ${className ?? ''}`} aria-hidden="true" />;
}

// ── 权限图标 ────────────────────────────────────────────────────

function DefaultPermissionIcon({ className }: { className?: string }) {
  return <span className={`chat-config-icon chat-config-icon--permission ${className ?? ''}`} aria-hidden="true" />;
}

function SafeAccessPermissionIcon({ className }: { className?: string }) {
  return <CircleAlert className={className} aria-hidden="true" />;
}

// ── 工作模式选项 ────────────────────────────────────────────────
// 只暴露面向用户的 2 种模式；auto_harness 不在此列

export const AGENT_MODE_OPTIONS: ChatOptionDef<AgentMode>[] = [
  {
    value: 'team',
    i18nKey: 'chat.config.mode.cluster',
    descriptionI18nKey: 'chat.config.mode.clusterDesc',
    icon: ClusterModeIcon,
  },
  {
    value: 'agent',
    i18nKey: 'chat.config.mode.singleAgent',
    icon: SingleAgentModeIcon,
  },
];

// ── 权限选项 ────────────────────────────────────────────────────

export const PERMISSION_OPTIONS: ChatOptionDef<Permission>[] = [
  {
    value: 'default',
    i18nKey: 'chat.config.permission.default',
    descriptionI18nKey: 'chat.config.permission.defaultDesc',
    icon: DefaultPermissionIcon,
  },
  {
    value: 'full_access',
    i18nKey: 'chat.config.permission.fullAccess',
    descriptionI18nKey: 'chat.config.permission.fullAccessDesc',
    icon: SafeAccessPermissionIcon,
  },
];
