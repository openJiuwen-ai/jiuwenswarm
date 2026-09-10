const ACCESS_KEY = 'openjiuwen_access_token';
const REFRESH_KEY = 'openjiuwen_refresh_token';

interface TokenBundle {
  access_token: string;
  refresh_token: string;
}

let refreshInFlight: Promise<boolean> | null = null;
let redirectStarted = false;

function storageValue(key: string): string | null {
  return typeof localStorage === 'undefined' ? null : localStorage.getItem(key);
}

function syncAccessCookie(accessToken: string | null): void {
  if (typeof document === 'undefined') return;
  const secure = typeof window !== 'undefined' && window.location.protocol === 'https:' ? '; Secure' : '';
  if (accessToken) {
    document.cookie = `${ACCESS_KEY}=${encodeURIComponent(accessToken)}; Path=/; SameSite=Strict${secure}`;
  } else {
    document.cookie = `${ACCESS_KEY}=; Path=/; Max-Age=0; SameSite=Strict${secure}`;
  }
}

export function getManagerAccessToken(): string | null {
  return storageValue(ACCESS_KEY);
}

export function getManagerRefreshToken(): string | null {
  return storageValue(REFRESH_KEY);
}

export function hasManagerSessionCredentials(): boolean {
  return Boolean(getManagerAccessToken() || getManagerRefreshToken());
}

export function setManagerTokens(accessToken: string, refreshToken: string): void {
  if (typeof localStorage !== 'undefined') {
    localStorage.setItem(ACCESS_KEY, accessToken);
    localStorage.setItem(REFRESH_KEY, refreshToken);
  }
  syncAccessCookie(accessToken);
  redirectStarted = false;
}

export function clearManagerTokens(): void {
  if (typeof localStorage !== 'undefined') {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  }
  syncAccessCookie(null);
}

export function redirectToManagerLogin(): boolean {
  clearManagerTokens();
  if (typeof window === 'undefined' || /^\/auth\/?$/.test(window.location.pathname)) {
    return false;
  }
  if (!redirectStarted) {
    redirectStarted = true;
    window.location.replace('/auth');
  }
  return true;
}

function withCurrentAccessToken(init: RequestInit, accessToken: string | null): RequestInit {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set('Authorization', `Bearer ${accessToken}`);
  else headers.delete('Authorization');
  return { ...init, headers };
}

function isTokenBundle(value: unknown): value is TokenBundle {
  if (!value || typeof value !== 'object') return false;
  const bundle = value as Record<string, unknown>;
  return (
    typeof bundle.access_token === 'string' &&
    Boolean(bundle.access_token.trim()) &&
    typeof bundle.refresh_token === 'string' &&
    Boolean(bundle.refresh_token.trim())
  );
}

async function performRefresh(): Promise<boolean> {
  const refreshToken = getManagerRefreshToken();
  const accessBeforeRefresh = getManagerAccessToken();
  if (!refreshToken) return false;
  try {
    const response = await fetch('/idp/v1/auth/refresh', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ refresh_token: refreshToken }),
    });
    if (!response.ok) {
      return getManagerRefreshToken() !== refreshToken && getManagerAccessToken() !== accessBeforeRefresh;
    }
    const body: unknown = await response.json();
    if (!isTokenBundle(body)) return false;
    setManagerTokens(body.access_token, body.refresh_token);
    return true;
  } catch {
    return false;
  }
}

export function refreshManagerSession(): Promise<boolean> {
  if (refreshInFlight) return refreshInFlight;
  refreshInFlight = performRefresh().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

/**
 * Send one request with the current access token. A first 401 performs a
 * single-flight refresh and retries exactly once with the rotated token pair.
 */
export async function managerAuthenticatedFetch(input: RequestInfo | URL, init: RequestInit = {}): Promise<Response> {
  const accessUsed = getManagerAccessToken();
  const response = await fetch(input, withCurrentAccessToken(init, accessUsed));
  if (response.status !== 401) return response;

  // Another concurrent request may already have refreshed while this request
  // was in flight. Retry with that token without rotating refresh again.
  const currentAccess = getManagerAccessToken();
  if (currentAccess && currentAccess !== accessUsed) {
    return fetch(input, withCurrentAccessToken(init, currentAccess));
  }

  if (await refreshManagerSession()) {
    return fetch(input, withCurrentAccessToken(init, getManagerAccessToken()));
  }

  redirectToManagerLogin();
  return response;
}
