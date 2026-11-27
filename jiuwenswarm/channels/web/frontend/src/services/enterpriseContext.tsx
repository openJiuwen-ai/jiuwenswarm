import { createContext, useContext } from 'react';

export type EnterpriseUser = { user_id: string; display_name: string };

/** Manager 返回的可访问上下文；任选一项即可访问对应 Agent。 */
export type EnterpriseAgentContext = {
  bot_id: string;
  group_id: string;
  user_id: string;
  jiuwenclaw_id: string;
  /** 实例/集群展示名（对应 Manager instance_info.jiuwenclaw_name） */
  jiuwenclaw_name: string;
  /** 平台展示名：agent_{runtime_template_id}_{agent_template_id}；旧 Manager 为 resource_name */
  agent_name: string;
  /** "{runtime_template_id}_{agent_template_id}"；旧 Manager 缺省 */
  agent_id?: string;
  /** 运行时模板 id；无匹配运行时模板时缺省 */
  runtime_template_id?: string;
  /** Agent 模板 id；旧 Manager 缺省 */
  agent_template_id?: string;
  /** 当前 Agent 配置模板是否允许上传本地 Skill；旧 Manager 缺省按 false */
  allow_local_skill_upload?: boolean;
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
  onLogout: () => void;
};

export function agentContextKey(
  item: Pick<EnterpriseAgentContext, 'bot_id' | 'group_id' | 'user_id' | 'jiuwenclaw_id'>,
): string {
  return `${item.bot_id}\u0001${item.group_id}\u0001${item.user_id}\u0001${item.jiuwenclaw_id}`;
}

export const EnterpriseContext = createContext<EnterpriseContextValue | null>(null);
export function useEnterpriseContext() {
  return useContext(EnterpriseContext);
}
