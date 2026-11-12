import { getApiPrefix } from '../../utils/env';

export function userWebPaths(pathname = window.location.pathname) {
  const configuredPrefix = [import.meta.env?.BASE_URL, getApiPrefix()]
    .map((base) => base?.replace(/\/+$/, '') || '')
    .find((base) => /^\/(?![\/\\])[^\\?#]*$/.test(base)
      && (pathname === base || pathname.startsWith(`${base}/`)));
  // Legacy portal URLs can carry a prefix even with a relative Vite build base.
  const routeIndex = pathname.search(/\/(?:chat|auth)(?:\/|$)/);
  const legacyPrefix = routeIndex < 0
    ? pathname.replace(/\/(?:index\.html)?$/, '')
    : pathname.slice(0, routeIndex);
  const prefix = configuredPrefix || (
    legacyPrefix !== pathname && /^\/(?![\/\\])[^\\]*$/.test(legacyPrefix) ? legacyPrefix : ''
  );
  return { root: `${prefix}/`, login: `${prefix}/auth`, route: pathname.slice(prefix.length) || '/' };
}

export function isUserLoginRoute(route: string): boolean {
  return route === '/auth' || route.startsWith('/auth/');
}
