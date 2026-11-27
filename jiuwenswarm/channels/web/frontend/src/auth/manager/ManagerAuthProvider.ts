import type { EnterpriseAgentContext, EnterpriseUser } from '../../services/enterpriseContext';
import { EnterpriseAuthError, type EnterpriseAuthProvider } from '../types';
import {
  clearActiveUserCluster,
  clearManagerTokens,
  getManagerAccessToken,
  getManagerRefreshToken,
  managerAuthenticatedFetch,
  redirectToManagerLogin,
} from './authSession';
import { userWebPaths } from './userWebPaths';
import i18n from '../../i18n';

interface ManagerResponse<T> {
  code: number;
  message?: string;
  data: T;
}

function requestMessage(body: unknown, fallback: string): string {
  if (body && typeof body === 'object') {
    const value = body as Record<string, unknown>;
    if (typeof value.detail === 'string' && value.detail.trim()) return value.detail;
    if (typeof value.message === 'string' && value.message.trim()) return value.message;
  }
  return fallback;
}

async function requestJson<T>(path: string, init: RequestInit = {}): Promise<T> {
  let response: Response;
  try {
    response = await managerAuthenticatedFetch(path, init);
  } catch (error) {
    throw new EnterpriseAuthError(0, i18n.t('auth.manager.networkFailed', {
      message: error instanceof Error ? error.message : String(error),
    }));
  }
  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    // The HTTP status remains authoritative for non-JSON responses.
  }
  if (!response.ok) throw new EnterpriseAuthError(response.status, requestMessage(body, `HTTP ${response.status}`));
  return body as T;
}

export const managerAuthProvider: EnterpriseAuthProvider = {
  id: 'manager',
  startupMessage: i18n.t('auth.startup.manager'),
  isAuthenticated: () => Boolean(getManagerAccessToken()),
  redirectToLogin() {
    return redirectToManagerLogin();
  },
  // Authentication and user-console endpoints share the dedicated user origin.
  async getCurrentUser() {
    return requestJson<EnterpriseUser>('/idp/v1/auth/me');
  },
  async listAgentContexts() {
    const result = await requestJson<ManagerResponse<{
      contexts: EnterpriseAgentContext[];
      empty_reason?: 'no_grant' | 'no_agent_template' | null;
    }>>('/manager-api/v1/user-console/agent-contexts');
    if (result.code !== 200) throw new EnterpriseAuthError(result.code, result.message || i18n.t('auth.manager.contextFailed'));
    return {
      contexts: result.data?.contexts ?? [],
      emptyReason: result.data?.empty_reason ?? null,
    };
  },
  async setActiveCluster(jiuwenclawId: string) {
    const result = await requestJson<ManagerResponse<{ jiuwenclaw_id: string }>>(
      '/manager-api/v1/user-console/active-cluster',
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ jiuwenclaw_id: jiuwenclawId }),
      },
    );
    if (result.code !== 200) {
      throw new EnterpriseAuthError(result.code, result.message || i18n.t('auth.manager.switchClusterFailed'));
    }
  },
  async logout() {
    if (localStorage.getItem('openjiuwen_oidc_connection')) {
      window.location.assign(`${userWebPaths().login}/federation/logout`);
      return;
    }
    const refreshToken = getManagerRefreshToken();
    if (refreshToken) {
      try {
        await fetch('/idp/v1/auth/logout', {
          method: 'POST',
          credentials: 'include',
          headers: {
            ...(getManagerAccessToken() ? { Authorization: `Bearer ${getManagerAccessToken()}` } : {}),
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
      } catch {
        // Local logout must still complete when the identity service is unavailable.
      }
    }
    await clearActiveUserCluster();
    clearManagerTokens();
    this.redirectToLogin();
  },
};
