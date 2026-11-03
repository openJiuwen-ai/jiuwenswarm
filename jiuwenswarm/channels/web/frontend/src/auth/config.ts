const LOGIN_AUTH_SIMULATE_DEFAULT = true;

export function parseLoginAuthSimulate(raw: unknown): boolean {
  if (raw === undefined || raw === null || raw === '') return LOGIN_AUTH_SIMULATE_DEFAULT;
  const normalized = String(raw).trim().toLowerCase();
  if (normalized === 'true') return true;
  if (normalized === 'false') return false;
  throw new Error(`LOGIN_AUTH_SIMULATE 配置非法：期望 true 或 false，实际为 ${String(raw)}`);
}

export function isLoginAuthSimulateEnabled(): boolean {
  const injected = window.__JIUWEN_LOGIN_AUTH_SIMULATE__;
  if (injected !== undefined && injected !== '__JIUWEN_LOGIN_AUTH_SIMULATE_VALUE__') {
    return parseLoginAuthSimulate(injected);
  }
  return parseLoginAuthSimulate(import.meta.env.VITE_LOGIN_AUTH_SIMULATE);
}

export function isAuthEntryPath(pathname: string): boolean {
  return pathname === '/auth' || pathname === '/auth/';
}
