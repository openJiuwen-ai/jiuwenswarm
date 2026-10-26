import type { SettingsRequest } from './settingsContract';

export type SettingsRequestRoute = {
  id: string;
  methods: readonly string[];
  request: SettingsRequest;
};

export function createSettingsRequestRouter(routes: readonly SettingsRequestRoute[]): SettingsRequest {
  if (routes.length === 0) throw new Error('Settings request router requires at least one route');
  const requestsByMethod = new Map<string, SettingsRequest>();
  const routeIds = new Set<string>();
  for (const route of routes) {
    if (!route.id.trim()) throw new Error('Settings request route id must not be empty');
    if (routeIds.has(route.id)) throw new Error(`Duplicate settings request route id: ${route.id}`);
    routeIds.add(route.id);
    if (route.methods.length === 0) throw new Error(`Settings request route ${route.id} must register methods`);
    for (const method of route.methods) {
      if (!method.trim()) throw new Error(`Settings request route ${route.id} contains an empty method`);
      if (requestsByMethod.has(method)) throw new Error(`Duplicate settings request method: ${method}`);
      requestsByMethod.set(method, route.request);
    }
  }
  return (method, params, options) => {
    const request = requestsByMethod.get(method);
    if (!request) return Promise.reject(new Error(`No settings request route registered for method: ${method}`));
    return request(method, params, options);
  };
}
