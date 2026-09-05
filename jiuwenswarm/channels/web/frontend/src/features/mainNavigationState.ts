export type MainNavKey =
  | 'chat'
  | 'skills'
  | 'agents'
  | 'teams'
  | 'sessions'
  | 'history'
  | 'cron'
  | 'channels'
  | 'extensions'
  | 'configpanel'
  | 'browserpanel'
  | 'updatepanel'
  | 'a2aingress';

export const MAIN_NAV_STORAGE_KEY = 'jiuwenswarm.active-main-nav';

const MAIN_NAV_KEYS = new Set<MainNavKey>([
  'chat',
  'skills',
  'agents',
  'teams',
  'sessions',
  'history',
  'cron',
  'channels',
  'extensions',
  'configpanel',
  'browserpanel',
  'updatepanel',
  'a2aingress',
]);

export function parseStoredMainNav(raw: string | null, options: { blocked?: readonly MainNavKey[]; updaterEnabled?: boolean } = {}): MainNavKey {
  const nav = String(raw || '').trim() as MainNavKey;
  if (!MAIN_NAV_KEYS.has(nav)) return 'chat';
  if (options.blocked?.includes(nav)) return 'chat';
  if (nav === 'updatepanel' && options.updaterEnabled === false) return 'chat';
  return nav;
}

type MainNavRouteState = {
  previousRouteKey: string;
  routeKey: string;
  routeKind: 'chat-new' | 'chat-session' | 'not-found';
};

/** 首次挂载不覆盖已恢复的面板；用户真正切换会话路由时回到工作页。 */
export function resolveMainNavAfterRoute(current: MainNavKey, route: MainNavRouteState): MainNavKey {
  if (route.routeKind === 'not-found') return 'chat';
  return route.previousRouteKey === route.routeKey ? current : 'chat';
}
