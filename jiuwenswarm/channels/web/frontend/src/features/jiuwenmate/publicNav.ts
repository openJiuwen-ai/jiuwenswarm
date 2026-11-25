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
  { nav: 'settings', labelKey: 'jiuwenmate.nav.settings' },
];

/** Assets (personal context) stays hidden until its switch is on, like the personal rail. */
export function visibleJiuwenMatePublicPages(
  hidden: readonly JiuwenMateExistingPage[],
): readonly JiuwenMatePublicNavItem[] {
  return JIUWENMATE_PUBLIC_PAGES.filter((item) => !hidden.includes(item.nav));
}

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
