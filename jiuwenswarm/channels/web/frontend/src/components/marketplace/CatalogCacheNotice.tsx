import { useTranslation } from 'react-i18next';
import type { CatalogCacheMetadata } from '../../features/catalogCache';
export function CatalogCacheNotice({ cache }: { cache?: CatalogCacheMetadata }) {
  const { i18n } = useTranslation();
  if (!cache || (!cache.refreshing && cache.state === 'fresh' && cache.complete !== false)) return null;
  const zh = i18n.language.startsWith('zh');
  const text = cache.refreshing
    ? zh
      ? '正在更新目录，已有内容仍可使用。'
      : 'Updating the catalog. Existing items remain available.'
    : cache.state === 'error' || cache.last_refresh_error
      ? zh
        ? '目录更新暂时失败，保留上次可用内容。'
        : 'Catalog refresh failed. Previously available items are retained.'
      : cache.complete === false
        ? zh
          ? '当前显示部分目录。'
          : 'Showing a partial catalog.'
        : zh
          ? '当前显示已缓存的目录。'
          : 'Showing the cached catalog.';
  return (
    <p
      className="page-shell py-2 text-xs text-text-muted"
      role="status"
      data-testid="marketplace-cache-notice"
      data-variant={cache.state}
    >
      {text}{' '}
      {(cache.updated_at || cache.fetched_at) && (
        <time data-testid="marketplace-cache-updated" dateTime={cache.updated_at || cache.fetched_at}>
          {cache.updated_at || cache.fetched_at}
        </time>
      )}
    </p>
  );
}
