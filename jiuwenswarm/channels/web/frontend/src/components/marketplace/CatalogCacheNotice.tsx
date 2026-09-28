import { useTranslation } from 'react-i18next';
import {
  catalogCacheNotice,
  catalogCacheTimestamp,
  formatCatalogCacheUpdatedAt,
  type CatalogCacheMetadata,
} from '../../features/catalogCache';
export function CatalogCacheNotice({ cache }: { cache?: CatalogCacheMetadata }) {
  const { i18n } = useTranslation();
  const notice = catalogCacheNotice(cache);
  if (!notice) return null;
  const zh = i18n.language.startsWith('zh');
  const text = notice.kind === 'error'
    ? zh
      ? '目录刷新失败，继续显示上次可用内容。'
      : 'Catalog refresh failed. Previously available items are retained.'
    : zh
      ? '当前显示旧缓存。'
      : 'Showing previously cached content.';
  const updatedText = formatCatalogCacheUpdatedAt(notice.updatedAt, i18n.language);
  const updatedTimestamp = catalogCacheTimestamp(notice.updatedAt);
  return (
    <p
      className="page-shell py-2 text-xs text-text-muted"
      role="status"
      data-testid="marketplace-cache-notice"
      data-variant={notice.kind}
    >
      {text}
      {updatedText && updatedTimestamp !== null && (
        <time data-testid="marketplace-cache-updated" dateTime={new Date(updatedTimestamp).toISOString()}>
          {zh ? ` 上次更新时间：${updatedText}` : ` Last updated: ${updatedText}`}
        </time>
      )}
    </p>
  );
}
