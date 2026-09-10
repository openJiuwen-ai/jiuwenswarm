import type { EnterpriseAgentContext, EnterpriseUser } from '../services/enterpriseContext';

export interface EnterpriseAuthProvider {
  readonly id: 'manager' | 'simulate';
  readonly startupMessage: string;
  isAuthenticated(): boolean;
  /** Return false when the User Web itself has already fallen back to /auth. */
  redirectToLogin(): boolean;
  getCurrentUser(): Promise<EnterpriseUser>;
  listAgentContexts(): Promise<EnterpriseAgentContext[]>;
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
