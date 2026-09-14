import type { EnterpriseAgentContext, EnterpriseUser } from '../services/enterpriseContext';

export interface EnterpriseAuthProvider {
  readonly id: 'manager' | 'simulate';
  readonly startupMessage: string;
  isAuthenticated(): boolean;
  /** Return false when the User Web itself has already fallen back to /auth. */
  redirectToLogin(): boolean;
  getCurrentUser(): Promise<EnterpriseUser>;
  listAgentContexts(): Promise<EnterpriseAgentContext[]>;
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
