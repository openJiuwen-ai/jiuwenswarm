import { createContext, useContext } from 'react';

export type EnterpriseUser = { user_id: string; display_name: string };

/** Manager 返回的可访问上下文；任选一项即可访问对应 Agent。 */
export type EnterpriseAgentContext = {
  bot_id: string;
  group_id: string;
  user_id: string;
  jiuwenclaw_id: string;
  agent_name: string;
  group_name: string;
};

export type EnterpriseContextSnapshot = {
  user: EnterpriseUser;
  contexts: EnterpriseAgentContext[];
  selected: EnterpriseAgentContext;
};

export type EnterpriseContextValue = EnterpriseContextSnapshot & {
  contextError: string;
  contextSwitching: boolean;
  onContextChange: (key: string) => void;
  onCustomContextApply: (input: { botId: string; groupId: string; userId: string }) => void;
  onLogout: () => void;
};

export function agentContextKey(item: Pick<EnterpriseAgentContext, 'bot_id' | 'group_id' | 'user_id'>): string {
  return `${item.bot_id}\u0001${item.group_id}\u0001${item.user_id}`;
}

export function formatAgentContextLabel(item: EnterpriseAgentContext): string {
  const agentName = item.agent_name?.trim() || item.bot_id;
  const groupName = item.group_name?.trim() || item.group_id;
  return `${agentName}-${groupName}`;
}

export const EnterpriseContext = createContext<EnterpriseContextValue | null>(null);
export function useEnterpriseContext() {
  return useContext(EnterpriseContext);
}
