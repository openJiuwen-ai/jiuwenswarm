import { CircleAlert } from 'lucide-react';
import type { AgentMode, Permission } from '../types';

export interface ChatOptionDef<T extends string> {
  value: T;
  i18nKey: string;
  descriptionI18nKey?: string;
  icon: (props: { className?: string }) => JSX.Element;
  hidden?: boolean;
}

// ── 工作模式图标 ────────────────────────────────────────────────

function ClusterModeIcon({ className }: { className?: string }) {
  return <span className={`chat-config-icon chat-config-icon--cluster ${className ?? ''}`} aria-hidden="true" />;
}

function SingleAgentModeIcon({ className }: { className?: string }) {
  return <span className={`chat-config-icon chat-config-icon--single-agent ${className ?? ''}`} aria-hidden="true" />;
}

function AutoModeIcon({ className }: { className?: string }) {
  return (
    <svg className={className} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={2} aria-hidden="true">
      <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
    </svg>
  );
}

// ── 权限图标 ────────────────────────────────────────────────────

function DefaultPermissionIcon({ className }: { className?: string }) {
  return <span className={`chat-config-icon chat-config-icon--permission ${className ?? ''}`} aria-hidden="true" />;
}

function SafeAccessPermissionIcon({ className }: { className?: string }) {
  return <CircleAlert className={className} aria-hidden="true" />;
}

// ── 工作模式选项 ────────────────────────────────────────────────
// Align with develop: Agent | Cluster | Auto (opt-in). Plan is a separate toggle on Agent.

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
  {
    value: 'auto',
    i18nKey: 'chat.modeAuto',
    icon: AutoModeIcon,
  },
  {
    value: 'auto_harness',
    i18nKey: 'chat.modeAutoHarness',
    descriptionI18nKey: 'chat.modeAutoHarnessDesc',
    icon: AutoModeIcon,
    hidden: true,
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
