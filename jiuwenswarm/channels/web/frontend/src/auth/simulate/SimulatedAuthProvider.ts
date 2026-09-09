import {
  type EnterpriseAgentContext,
  type EnterpriseContextSnapshot,
} from '../../services/enterpriseContext';
import { parseRuntimeScope } from '../../services/runtimeScope';
import type { EnterpriseAuthProvider } from '../types';

const DEFAULTS = {
  userId: 'default',
  displayName: 'Debug User',
  groupId: 'default',
  groupName: 'Debug Organization',
  gatewayId: 'debug-gateway',
  botId: 'default',
  agentName: 'Debug Agent',
} as const;

function buildContext(search = ''): EnterpriseAgentContext {
  const preferred = parseRuntimeScope(search);
  const botId = preferred.botId || DEFAULTS.botId;
  const groupId = preferred.groupId || DEFAULTS.groupId;
  return {
    bot_id: botId,
    group_id: groupId,
    user_id: preferred.userId || DEFAULTS.userId,
    jiuwenclaw_id: DEFAULTS.gatewayId,
    agent_name: DEFAULTS.agentName,
    group_name: DEFAULTS.groupName,
  };
}

export function buildSimulatedEnterpriseContext(search = ''): EnterpriseContextSnapshot {
  const selected = buildContext(search);
  return {
    user: { user_id: selected.user_id, display_name: DEFAULTS.displayName },
    contexts: [selected],
    selected,
  };
}

function entryPath(): string {
  return window.location.pathname.startsWith('/chat') ? '/chat/' : '/';
}

export const simulatedAuthProvider: EnterpriseAuthProvider = {
  id: 'simulate',
  startupMessage: '【登录认证模拟调试模式已开启】使用默认 Agent 上下文候选值',
  isAuthenticated: () => true,
  redirectToLogin: () => {
    window.location.replace(entryPath());
    return true;
  },
  getCurrentUser: async () => buildSimulatedEnterpriseContext(window.location.search).user,
  listAgentContexts: async () => buildSimulatedEnterpriseContext(window.location.search).contexts,
  async logout() {
    window.location.replace(entryPath());
  },
};
