import type { EnterpriseAgentContext, EnterpriseUser } from '../services/enterpriseContext';

/** 空态原因：无授权 / 未配置 Agent 模板 */
export type AgentContextsEmptyReason = 'no_grant' | 'no_agent_template';

export interface AgentContextsResult {
  contexts: EnterpriseAgentContext[];
  emptyReason?: AgentContextsEmptyReason | null;
}

export interface EnterpriseAuthProvider {
  readonly id: 'manager' | 'simulate';
  readonly startupMessage: string;
  isAuthenticated(): boolean;
  /** Return false when the User Web itself has already fallen back to /auth. */
  redirectToLogin(): boolean;
  getCurrentUser(): Promise<EnterpriseUser>;
  listAgentContexts(): Promise<EnterpriseAgentContext[] | AgentContextsResult>;
  /** 写入 Manager 用户面反代 Cookie ``jiuwenclaw_id``；模拟登录可省略。 */
  setActiveCluster?(jiuwenclawId: string): Promise<void>;
  logout(): Promise<void>;
}

export class EnterpriseAuthError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'EnterpriseAuthError';
  }
}
