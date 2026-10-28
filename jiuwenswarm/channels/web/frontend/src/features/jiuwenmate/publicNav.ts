import type { JiuwenMateExistingPage } from './types';

export interface JiuwenMatePublicNavItem {
  nav: JiuwenMateExistingPage;
  labelKey: string;
}

export const JIUWENMATE_PUBLIC_PAGES: readonly JiuwenMatePublicNavItem[] = [
  { nav: 'agents', labelKey: 'jiuwenmate.nav.plaza' },
  { nav: 'personalContext', labelKey: 'jiuwenmate.nav.assets' },
  { nav: 'skills', labelKey: 'jiuwenmate.nav.skills' },
  { nav: 'connectorMarket', labelKey: 'jiuwenmate.nav.extensions' },
  { nav: 'personalContext', labelKey: 'jiuwenmate.nav.knowledge' },
  { nav: 'settings', labelKey: 'jiuwenmate.nav.settings' },
];

export interface JiuwenMateNavigationState<TNav extends string = string> {
  isJiuwenMate: boolean;
  activeNav: TNav;
}

export interface JiuwenMateEmbeddedNavigationState<TNav extends string = string> extends JiuwenMateNavigationState<TNav> {
  embeddedPage: JiuwenMateExistingPage | null;
}

export function openJiuwenMatePublicPage<TNav extends string>(
  state: JiuwenMateNavigationState<TNav>,
  page: JiuwenMateExistingPage,
): JiuwenMateEmbeddedNavigationState<TNav> {
  return { ...state, isJiuwenMate: true, embeddedPage: page };
}

export function reenterJiuwenMate<TNav extends string>(
  state: JiuwenMateNavigationState<TNav>,
): JiuwenMateNavigationState<TNav> {
  return { ...state, isJiuwenMate: true };
}
