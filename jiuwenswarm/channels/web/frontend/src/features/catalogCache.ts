export type CatalogCacheMetadata = {
  state: 'fresh' | 'stale' | 'miss' | 'error';
  refreshing: boolean;
  updated_at?: string;
  fetched_at?: string;
  complete?: boolean;
  has_more?: boolean;
  revision?: string;
  last_refresh_error?: unknown;
};
export type CatalogItems<T> = T[] & { cache?: CatalogCacheMetadata };
export function withCatalogCache<T>(items: T[], cache?: CatalogCacheMetadata): CatalogItems<T> {
  return Object.assign(items, { cache });
}
export function catalogScope(): string {
  try {
    return `${sessionStorage.getItem('marketplace_oauth_provider') || ''}:${sessionStorage.getItem('marketplace_oauth_access_token') || ''}`;
  } catch {
    return '';
  }
}
const refreshes = new Map<string, { timer: ReturnType<typeof setTimeout>; count: number }>();
/** Poll only an in-progress local cache refresh; retain cards and stop after two minutes. */
export function scheduleCatalogRefresh(
  key: string,
  cache: CatalogCacheMetadata | undefined,
  refresh: () => void,
  isCurrent: () => boolean,
) {
  const previous = refreshes.get(key);
  if (previous) clearTimeout(previous.timer);
  if (!cache?.refreshing || (previous?.count || 0) >= 30) {
    refreshes.delete(key);
    return;
  }
  const scope = catalogScope();
  const timer = setTimeout(() => {
    if (scope === catalogScope() && isCurrent()) refresh();
    else refreshes.delete(key);
  }, 4000);
  refreshes.set(key, { timer, count: (previous?.count || 0) + 1 });
}
export function catalogCacheOf(items: unknown): CatalogCacheMetadata | undefined {
  return (items as { cache?: CatalogCacheMetadata } | null)?.cache;
}
