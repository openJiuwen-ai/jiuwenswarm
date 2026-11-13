import { resolveApiUrl } from '../../utils/env';
import { managerAuthenticatedFetch, setManagerTokens } from './authSession';

export interface FederationConnection {
  connection_id: string;
  name: string;
  flow: 'browser_pkce' | 'server_code';
}

export interface FederationConfiguration {
  connection_id: string;
  flow: 'browser_pkce';
  authority: string;
  client_id: string;
  scope: string;
  automatic_silent_renew: boolean;
}

interface TokenBundle {
  access_token: string;
  refresh_token: string;
  federation?: { connection_id: string; user_id: string; access_token: string; token_type: string };
}

const AUTH = '/idp/v1/auth';

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(resolveApiUrl(AUTH + path), { credentials: 'same-origin', ...init });
  if (!response.ok) throw new Error(`Authentication request failed (${response.status})`);
  return response.json() as Promise<T>;
}

function json(body: unknown, authorization?: string): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(authorization ? { Authorization: authorization } : {}) },
    body: JSON.stringify(body),
  };
}

function saveSession(bundle: TokenBundle): void {
  setManagerTokens(bundle.access_token, bundle.refresh_token);
  setFederationToken(bundle.federation);
}

export function setFederationToken(token?: TokenBundle['federation']): void {
  if (token) localStorage.setItem('openjiuwen_federation', JSON.stringify(token));
  else localStorage.removeItem('openjiuwen_federation');
}

export const AuthApi = {
  async signIn(username: string, password: string): Promise<void> {
    saveSession(await request<TokenBundle>('/token?surface=user', {
      method: 'POST',
      body: new URLSearchParams({ username, password }),
    }));
    localStorage.removeItem('openjiuwen_oidc_connection');
    localStorage.removeItem('openjiuwen_oidc_renew');
  },
  async federationConnections(): Promise<FederationConnection[]> {
    return (await request<{ connections: FederationConnection[] }>('/federation/connections?surface=user')).connections;
  },
  federationConfiguration(connectionId: string): Promise<FederationConfiguration> {
    return request(`/federation/${encodeURIComponent(connectionId)}/configuration?surface=user`);
  },
  beginFederatedLogin(connectionId: string): void {
    window.location.assign(resolveApiUrl(`${AUTH}/federation/${encodeURIComponent(connectionId)}/login?surface=user`));
  },
  async confirmFederationToken(connectionId: string, token: string): Promise<void> {
    saveSession(await request<TokenBundle>(
      `/federation/${encodeURIComponent(connectionId)}/token-login?surface=user`,
      json({}, `Bearer ${token}`),
    ));
  },
  async exchangeFederationCode(code: string): Promise<void> {
    saveSession(await request<TokenBundle>('/federation/exchange?surface=user', json({ code })));
    localStorage.removeItem('openjiuwen_oidc_connection');
    localStorage.removeItem('openjiuwen_oidc_renew');
  },
  async refreshFederationHandoff(connectionId: string, token: string): Promise<TokenBundle['federation']> {
    const init = json({});
    const response = await managerAuthenticatedFetch(resolveApiUrl(
      `${AUTH}/federation/${encodeURIComponent(connectionId)}/token-handoff`,
    ), { ...init, headers: { ...init.headers, 'X-User-Token': `Bearer ${token}` } });
    if (!response.ok) throw new Error('Could not renew the external session');
    return response.json();
  },
};
