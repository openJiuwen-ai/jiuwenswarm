/**
 * 启动标识、完成记录、候选提示与首帧记录（Gateway S_09 第 4 节）。
 * 存储不可用时按“未完成、首次访问”处理，门控仍有时限保证进入界面。
 */

import { isRecord, randomHex } from '../protocol/utils.ts';

export const LAUNCH_SESSION_KEY = 'workswarm.launch';
export const COMPLETED_LAUNCH_KEY = 'workswarm.splash.completedLaunch';
export const SPLASH_PLAYED_KEY = 'workswarm.splash.played';
/** Read by the inline script in index.html: '0' skips the first-paint neutral layer. */
export const SPLASH_EXPECTED_KEY = 'workswarm.splash.expected';
/** The package that last provided the colors part; its splash plays first unless the user saved an order. */
export const APPEARANCE_PROVIDER_KEY = 'workswarm.appearance.provider';
/** Read by the inline script in index.html: '1' keeps the first-paint neutral layer for the appearance wait. */
export const APPEARANCE_EXPECTED_KEY = 'workswarm.appearance.expected';
/** The identity part last shown, JSON; index.html uses it for the neutral layer, the tab title and icon. */
export const APPEARANCE_IDENTITY_KEY = 'workswarm.appearance.identity';
export const SPLASH_HINT_PATH = '/extension-splash/hint';

export type SplashHint = 'candidates' | 'none' | 'unknown';

/** The Web server's hint; `appearance` is null when unknown. */
export interface StartupHint {
  splash: SplashHint;
  appearance: boolean | null;
}

export const UNKNOWN_HINT: StartupHint = { splash: 'unknown', appearance: null };

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
export async function fetchStartupHint(fetcher: typeof fetch = globalThis.fetch): Promise<StartupHint> {
  try {
    const response = await fetcher(SPLASH_HINT_PATH, { cache: 'no-store' });
    if (!response.ok) return UNKNOWN_HINT;
    const body: unknown = await response.json();
    if (!isRecord(body) || typeof body.candidates !== 'boolean') return UNKNOWN_HINT;
    return {
      splash: body.candidates ? 'candidates' : 'none',
      appearance: typeof body.appearance === 'boolean' ? body.appearance : null,
    };
  } catch {
    return UNKNOWN_HINT;
  }
}
