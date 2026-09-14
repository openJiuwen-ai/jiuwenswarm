import type { AuthorizationResult, AuthorizationState } from '../../services/personalContextApi';

export const AUTHORIZATION_POLL_INTERVAL_MS = 2000;

type AuthorizationAction = 'start' | 'reauthorize' | 'authorizing';

type PollingRuntime = {
  now: () => number;
  setTimeout: (callback: () => Promise<void>, delay: number) => number;
  clearTimeout: (id: number) => void;
};

type AuthorizationPollingOptions = {
  expiresAt: string | null;
  readStatus: () => Promise<AuthorizationResult | null>;
  onTerminal: (result: AuthorizationResult) => void;
  onExpired: () => void;
};

const browserRuntime: PollingRuntime = {
  now: Date.now,
  setTimeout: (callback, delay) => window.setTimeout(() => void callback(), delay),
  clearTimeout: (id) => window.clearTimeout(id),
};

export function authorizationAction(state: AuthorizationState): AuthorizationAction {
  if (state === 'authorizing') return 'authorizing';
  if (state === 'not_authorized' || state === 'authorization_required') return 'start';
  return 'reauthorize';
}

export function safeHttpsUrl(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' ? url.toString() : null;
  } catch {
    return null;
  }
}

export function startAuthorizationPolling(
  options: AuthorizationPollingOptions,
  runtime: PollingRuntime = browserRuntime,
): () => void {
  const expiresAt = options.expiresAt ? Date.parse(options.expiresAt) : Number.NaN;
  let cancelled = false;
  let timerId: number | null = null;

  const expired = () => Number.isFinite(expiresAt) && runtime.now() >= expiresAt;
  const schedule = () => {
    if (cancelled) return;
    timerId = runtime.setTimeout(poll, AUTHORIZATION_POLL_INTERVAL_MS);
  };
  const poll = async () => {
    timerId = null;
    if (cancelled) return;
    if (expired()) {
      options.onExpired();
      return;
    }

    let result: AuthorizationResult | null = null;
    try {
      result = await options.readStatus();
    } catch {
      // 短暂读取失败交给下一轮重试，不制造并发轮询。
    }
    if (cancelled) return;
    if (result?.state === 'authorized' || result?.state === 'authorization_failed') {
      options.onTerminal(result);
      return;
    }
    if (expired()) {
      options.onExpired();
      return;
    }
    schedule();
  };

  if (expired()) {
    options.onExpired();
  } else {
    schedule();
  }

  return () => {
    cancelled = true;
    if (timerId !== null) runtime.clearTimeout(timerId);
    timerId = null;
  };
}
