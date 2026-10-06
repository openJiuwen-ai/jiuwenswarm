/** SkillHub 托管的 GitCode/GitHub 授权与当前会话凭据。 */

// ── sessionStorage keys ──
const TOKEN_KEY = 'marketplace_oauth_access_token';
const PROVIDER_KEY = 'marketplace_oauth_provider';
const USER_KEY = 'marketplace_oauth_user';
const AUTHORIZATION_CLOSE_GRACE_MS = 5000;

// ── 类型 ──
export type OAuthProvider = 'gitcode' | 'github';
export type HubOAuthAttempt = { authorize_url: string; flow: string; claim: string };

export type OAuthUser = {
  id: string;
  name: string;
  login: string;
  avatar_url: string;
  is_market_moderation_admin: boolean;
};

async function responseJson(response: Response): Promise<Record<string, any>> {
  const text = await response.text();
  if (!text.trim()) return {};
  try {
    return JSON.parse(text);
  } catch {
    return {};
  }
}

// ── 存取函数 ──
export function getStoredOAuthToken(): string | null {
  try {
    return sessionStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function getStoredOAuthProvider(): OAuthProvider {
  try {
    const p = (sessionStorage.getItem(PROVIDER_KEY) || '').trim().toLowerCase();
    return p === 'github' ? 'github' : 'gitcode';
  } catch {
    return 'gitcode';
  }
}

export function getStoredOAuthUser(): OAuthUser | null {
  try {
    const raw = sessionStorage.getItem(USER_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export async function beginHubOAuth(provider: OAuthProvider): Promise<HubOAuthAttempt> {
  const response = await fetch('/marketplace-oauth/hub/start', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ provider }),
  });
  const result = await responseJson(response);
  if (!response.ok || !result?.authorize_url || !result?.flow || !result?.claim)
    throw new Error('无法启动 Hub 授权，请检查本地服务。');
  return {
    authorize_url: String(result.authorize_url),
    flow: String(result.flow),
    claim: String(result.claim),
  };
}

export async function waitForHubOAuth(
  attempt: HubOAuthAttempt,
  signal?: AbortSignal,
  authorizationWindowClosed?: () => boolean,
): Promise<void> {
  let windowClosedAt: number | undefined;
  while (!signal?.aborted) {
    await new Promise<void>((resolve) => window.setTimeout(resolve, 1000));
    if (signal?.aborted) break;
    if (windowClosedAt === undefined && authorizationWindowClosed?.()) windowClosedAt = Date.now();
    const response = await fetch('/marketplace-oauth/hub/result', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ flow: attempt.flow, claim: attempt.claim }),
      signal,
    });
    const result = await responseJson(response);
    if (!response.ok) throw new Error('授权请求已过期，请重新登录。');
    if (result.status === 'pending') {
      // A closed popup can precede the completed Hub handoff; allow it to settle.
      if (windowClosedAt !== undefined && Date.now() - windowClosedAt >= AUTHORIZATION_CLOSE_GRACE_MS)
        throw new Error('授权窗口已关闭，请重新登录。');
      continue;
    }
    if (result.status !== 'complete' || !result.access_token) {
      const code = typeof result.error === 'string' ? result.error.toLowerCase() : '';
      if (code.includes('session_exchange_failed'))
        throw new Error('Hub 登录结果兑换失败，请重新授权；若仍失败请联系 Hub 管理员。');
      throw new Error('授权失败，请重试。');
    }
    sessionStorage.setItem(TOKEN_KEY, result.access_token);
    sessionStorage.setItem(PROVIDER_KEY, result.provider);
    if (result.user) {
      const profile = result.user;
      const login = profile.login || profile.username || '';
      sessionStorage.setItem(
        USER_KEY,
        JSON.stringify({
          id: String(profile.id || ''),
          name: profile.name || login,
          login,
          avatar_url: profile.avatar_url || profile.avatar || '',
          is_market_moderation_admin: false,
        }),
      );
    } else sessionStorage.removeItem(USER_KEY);
    sessionStorage.removeItem('oauth_error');
    window.dispatchEvent(new CustomEvent('oauth-callback-complete'));
    return;
  }
  throw new DOMException('OAuth cancelled', 'AbortError');
}
