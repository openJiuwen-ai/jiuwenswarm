export type FrontendPlatform = 'web' | 'harmony';

export type SidebarNavKey =
  | 'chat' | 'skills' | 'personalContext' | 'agents' | 'teams' | 'sessions'
  | 'cron' | 'channels' | 'extensions' | 'configpanel' | 'settings'
  | 'browserpanel' | 'personalContextSettings' | 'updatepanel'
  | 'connectorMarket';

export const DEFAULT_FRONTEND_PLATFORM: FrontendPlatform = 'web';

const PLATFORM_ALIASES: Record<string, FrontendPlatform | null> = {
  web: 'web',
  desktop: 'web',
  harmony: 'harmony',
  openharmony: 'harmony',
  default: null,
  production: null,
  development: null,
  test: null,
};

const HIDDEN_NAV_ITEMS_BY_PLATFORM: Record<FrontendPlatform, readonly SidebarNavKey[]> = {
  web: ['sessions'],
  harmony: ['teams', 'sessions', 'updatepanel'],
};

export function normalizeFrontendPlatform(value: unknown): FrontendPlatform | null {
  const key = String(value ?? '')
    .trim()
    .toLowerCase();
  if (!key) return null;
  return PLATFORM_ALIASES[key] ?? null;
}

export function resolveFrontendPlatform(...sources: unknown[]): FrontendPlatform {
  for (const source of sources) {
    const platform = normalizeFrontendPlatform(source);
    if (platform) return platform;
  }
  return DEFAULT_FRONTEND_PLATFORM;
}

export function getHiddenNavItemsForPlatform(platform: FrontendPlatform): SidebarNavKey[] {
  return [...HIDDEN_NAV_ITEMS_BY_PLATFORM[platform]];
}

declare global {
  interface Window {
    __JIWEN_PLATFORM__?: string;
  }
}
