import type { EnterpriseAgentContext, EnterpriseUser } from '../../services/enterpriseContext';
import { EnterpriseAuthError, type EnterpriseAuthProvider } from '../types';
import {
  clearManagerTokens,
  getManagerAccessToken,
  getManagerRefreshToken,
  managerAuthenticatedFetch,
  redirectToManagerLogin,
} from './authSession';

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

async function requestJson<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await managerAuthenticatedFetch(path);
  } catch (error) {
    throw new EnterpriseAuthError(0, `网络请求失败：${error instanceof Error ? error.message : String(error)}`);
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
  startupMessage: '【正式身份认证模式，依赖manager ID认证服务】',
  isAuthenticated: () => Boolean(getManagerAccessToken()),
  redirectToLogin() {
    return redirectToManagerLogin();
  },
  getCurrentUser: () => requestJson<EnterpriseUser>('/idp/v1/auth/me'),
  async listAgentContexts() {
    const result = await requestJson<ManagerResponse<{ contexts: EnterpriseAgentContext[] }>>(
      '/manager-api/v1/user-console/agent-contexts',
    );
    if (result.code !== 200) throw new EnterpriseAuthError(result.code, result.message || '加载 Agent 上下文失败');
    return result.data?.contexts ?? [];
  },
  async logout() {
    const refreshToken = getManagerRefreshToken();
    if (refreshToken) {
      try {
        await fetch('/idp/v1/auth/logout', {
          method: 'POST',
          headers: {
            ...(getManagerAccessToken()
              ? { Authorization: `Bearer ${getManagerAccessToken()}` }
              : {}),
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({ refresh_token: refreshToken }),
        });
      } catch {
        // Local logout must still complete when the identity service is unavailable.
      }
    }
    clearManagerTokens();
    this.redirectToLogin();
  },
};
