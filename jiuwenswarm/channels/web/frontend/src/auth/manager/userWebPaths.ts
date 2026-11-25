export function userWebPaths(pathname = window.location.pathname) {
  return { root: '/', login: '/auth', route: pathname || '/' };
}

export function isUserLoginRoute(route: string): boolean {
  return route === '/auth' || route.startsWith('/auth/');
}
