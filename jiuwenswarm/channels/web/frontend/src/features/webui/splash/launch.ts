/**
 * 启动标识、完成记录与候选提示（Gateway S_09 第 4 节）。
 * 存储不可用时按“未完成、首次访问”处理，门控仍有时限保证进入界面。
 */

import { randomHex } from '../protocol/utils.ts';

export const LAUNCH_SESSION_KEY = 'workswarm.launch';
export const COMPLETED_LAUNCH_KEY = 'workswarm.splash.completedLaunch';
export const SPLASH_PLAYED_KEY = 'workswarm.splash.played';
/** Read by the inline script in index.html: '0' skips the first-paint neutral layer. */
export const SPLASH_EXPECTED_KEY = 'workswarm.splash.expected';
export const APPEARANCE_PROVIDER_KEY = 'workswarm.appearance.provider';
export const SPLASH_HINT_PATH = '/extension-splash/hint';

export type SplashHint = 'candidates' | 'none' | 'unknown';

export interface SplashLaunch {
  id: string;
  completed: boolean;
  firstVisit: boolean;
  markCompleted(): void;
}

function storage(kind: 'localStorage' | 'sessionStorage'): Storage | null {
  try {
    return globalThis[kind] ?? (globalThis as { window?: Window }).window?.[kind] ?? null;
  } catch {
    return null;
  }
}

export function readItem(key: string, kind: 'localStorage' | 'sessionStorage' = 'localStorage'): string | null {
  try {
    return storage(kind)?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

export function writeItem(key: string, value: string | null, kind: 'localStorage' | 'sessionStorage' = 'localStorage'): void {
  try {
    const target = storage(kind);
    if (value === null) target?.removeItem(key);
    else target?.setItem(key, value);
  } catch {
    // Storage failures must not block entering the application.
  }
}

/**
 * Electron shares one identifier per start; a browser tab falls back to its session.
 *
 * The completion record lives where the identifier lives: a desktop start is
 * shared by its windows (local storage, replaced by the next start), a browser
 * launch belongs to one tab (session storage), so tabs never overwrite each other.
 */
export function resolveLaunch(desktopLaunchId?: string): SplashLaunch {
  const desktop = typeof desktopLaunchId === 'string' && desktopLaunchId ? desktopLaunchId : null;
  let id = desktop ?? readItem(LAUNCH_SESSION_KEY, 'sessionStorage');
  if (!id) {
    id = randomHex();
    writeItem(LAUNCH_SESSION_KEY, id, 'sessionStorage');
  }
  const launchId = id;
  const scope = desktop ? 'localStorage' : 'sessionStorage';
  return {
    id: launchId,
    completed: readItem(COMPLETED_LAUNCH_KEY, scope) === launchId,
    firstVisit: readItem(SPLASH_PLAYED_KEY) !== '1',
    markCompleted: () => writeItem(COMPLETED_LAUNCH_KEY, launchId, scope),
  };
}

/** Ask the local Web server; anything but a well-formed answer counts as unknown. */
export async function fetchSplashHint(fetcher: typeof fetch = globalThis.fetch): Promise<SplashHint> {
  try {
    const response = await fetcher(SPLASH_HINT_PATH, { cache: 'no-store' });
    if (!response.ok) return 'unknown';
    const body: unknown = await response.json();
    if (typeof body === 'object' && body !== null && typeof (body as { candidates?: unknown }).candidates === 'boolean') {
      return (body as { candidates: boolean }).candidates ? 'candidates' : 'none';
    }
    return 'unknown';
  } catch {
    return 'unknown';
  }
}
