export type CatalogCacheMetadata = {
  state: 'fresh' | 'stale' | 'miss' | 'error';
  refreshing: boolean;
  updated_at?: string | number;
  fetched_at?: string | number;
  complete?: boolean;
  has_more?: boolean;
  revision?: string;
  error?: unknown;
  last_refresh_error?: unknown;
};
export type CatalogCacheNoticeModel = {
  kind: 'stale' | 'error';
  updatedAt?: string | number;
};
export function catalogCacheNotice(cache?: CatalogCacheMetadata): CatalogCacheNoticeModel | null {
  if (!cache) return null;
  const failed = cache.state === 'error' || Boolean(cache.error || cache.last_refresh_error);
  if (!failed && cache.state !== 'stale') return null;
  return {
    kind: failed ? 'error' : 'stale',
    ...((cache.updated_at ?? cache.fetched_at) !== undefined
      ? { updatedAt: cache.updated_at ?? cache.fetched_at }
      : {}),
  };
}
export function catalogCacheTimestamp(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value < 1_000_000_000_000 ? value * 1000 : value;
  }
  if (typeof value !== 'string' || !value.trim()) return null;
  const numeric = Number(value);
  if (Number.isFinite(numeric)) return numeric < 1_000_000_000_000 ? numeric * 1000 : numeric;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : null;
}
export function formatCatalogCacheUpdatedAt(value: unknown, locale?: string, timeZone?: string): string {
  const timestamp = catalogCacheTimestamp(value);
  if (timestamp === null) return '';
  return new Intl.DateTimeFormat(locale, {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
    ...(timeZone ? { timeZone } : {}),
  }).format(timestamp);
}
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
/** Cold miss: poll quickly so the first paint is not delayed by a multi-second gap. */
const MISS_POLL_MS = 400;
/** Stale-while-revalidate: slower poll; cards are already visible. */
const STALE_POLL_MS = 4000;
const MISS_MAX_POLLS = 150; // ~60s at MISS_POLL_MS
const STALE_MAX_POLLS = 30; // ~120s at STALE_POLL_MS

/** True when prefer_cache returned no payload and a background fill is in flight. */
export function isCatalogMissRefreshing(cache?: CatalogCacheMetadata): boolean {
  return Boolean(cache && cache.state === 'miss' && cache.refreshing);
}

/** Poll only an in-progress local cache refresh; retain cards and stop after ~1–2 minutes. */
export function scheduleCatalogRefresh(
  key: string,
  cache: CatalogCacheMetadata | undefined,
  refresh: () => void,
  isCurrent: () => boolean,
) {
  const previous = refreshes.get(key);
  if (previous) clearTimeout(previous.timer);
  const miss = cache?.state === 'miss';
  const maxPolls = miss ? MISS_MAX_POLLS : STALE_MAX_POLLS;
  if (!cache?.refreshing || (previous?.count || 0) >= maxPolls) {
    refreshes.delete(key);
    return;
  }
  const scope = catalogScope();
  const delayMs = miss ? MISS_POLL_MS : STALE_POLL_MS;
  const timer = setTimeout(() => {
    if (scope === catalogScope() && isCurrent()) refresh();
    else refreshes.delete(key);
  }, delayMs);
  refreshes.set(key, { timer, count: (previous?.count || 0) + 1 });
}
export function catalogCacheOf(items: unknown): CatalogCacheMetadata | undefined {
  return (items as { cache?: CatalogCacheMetadata } | null)?.cache;
}
