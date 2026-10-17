import { useEffect, useRef } from 'react';
import { useTranslation } from 'react-i18next';
// 直接从 Toast 模块深引入(不走 ../ui barrel):本组件被 market-visual-contract 测试以 esbuild 独立打包,
// barrel 会连带拉入 ?react 等 Vite 专属导入。
import { toast } from '../ui/Toast/toastStore';
import {
  catalogCacheNotice,
  catalogCacheTimestamp,
  formatCatalogCacheUpdatedAt,
  type CatalogCacheMetadata,
} from '../../features/catalogCache';

/** 常驻缓存提示 toast 的 testId(DOM 契约,保持稳定);业务去重 id 按实例派生,多实例互不干扰。 */
const CACHE_NOTICE_TOAST_ID = 'marketplace-cache-notice';
let cacheNoticeToastSeq = 0;

/**
 * 目录缓存状态提示:stale/刷新失败时在右上角挂一条常驻 info toast(不自动消失,可手动关闭),
 * 缓存恢复 fresh 时自动收起;同 App.tsx 的连接状态 toast 模式。
 */
export function CatalogCacheNotice({ cache }: { cache?: CatalogCacheMetadata }) {
  const { i18n } = useTranslation();
  const notice = catalogCacheNotice(cache);
  const kind = notice?.kind;
  const updatedAt = notice?.updatedAt;
  const language = i18n.language;
  const toastKeyRef = useRef<number | null>(null);
  // 业务 id 按实例派生:同实例的原地更新共用一条;两个实例同时挂载时各持有一条,卸载互不影响
  const toastIdRef = useRef(`${CACHE_NOTICE_TOAST_ID}:${++cacheNoticeToastSeq}`);

  useEffect(() => {
    if (!kind) {
      if (toastKeyRef.current !== null) {
        toast.close(toastKeyRef.current);
        toastKeyRef.current = null;
      }
      return;
    }
    const zh = language.startsWith('zh');
    const text = kind === 'error'
      ? zh
        ? '目录刷新失败，继续显示上次可用内容。'
        : 'Catalog refresh failed. Previously available items are retained.'
      : zh
        ? '当前显示旧缓存。'
        : 'Showing previously cached content.';
    const updatedText = formatCatalogCacheUpdatedAt(updatedAt, language);
    const updatedTimestamp = catalogCacheTimestamp(updatedAt);
    toastKeyRef.current = toast.open({
      id: toastIdRef.current,
      content: (
        <>
          {text}
          {updatedText && updatedTimestamp !== null && (
            <time data-testid="marketplace-cache-updated" dateTime={new Date(updatedTimestamp).toISOString()}>
              {zh ? ` 上次更新时间：${updatedText}` : ` Last updated: ${updatedText}`}
            </time>
          )}
        </>
      ),
      variant: 'info',
      position: 'right',
      duration: 0,
      wide: true,
      testId: CACHE_NOTICE_TOAST_ID,
      // 恢复迁移到 toast 前内联 div 上的 data-variant={kind} 语义（stale/error），
      // 让外部自动化能按状态收窄该常驻提示；其它 toast 不传则仍取内部数字 key。
      dataVariant: kind,
    });
  }, [kind, updatedAt, language]);

  // 组件卸载(所在面板关闭/切页)时收起提示,不留孤儿 toast。
  useEffect(() => {
    return () => {
      if (toastKeyRef.current !== null) {
        toast.close(toastKeyRef.current);
        toastKeyRef.current = null;
      }
    };
  }, []);

  return null;
}
