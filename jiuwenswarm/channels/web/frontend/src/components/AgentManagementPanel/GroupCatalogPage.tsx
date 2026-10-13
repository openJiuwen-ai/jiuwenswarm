import { useCallback, useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { AgentGroupCatalogItem, RequestStatus } from '../../features/agentManagement';
import { CategoryTabs, EmptyState } from '../ui';
// 深引入而非 ../ui barrel：本组件会被 esbuild 独立打包进测试
import { LoadingSpinner } from '../ui/LoadingSpinner/LoadingSpinner';
import { GroupCard } from './GroupCard';

const GROUP_CATEGORIES = [
  'ProductDevelopment',
  'Marketing',
  'Efficiency',
  'DataAnalysis',
  'ContentCreation',
  'SafetyCompliance',
  'Communication',
  'Other',
];

/** 首批渲染数量；触底后每批追加同数量（组目录一次性载入内存，这里做增量展示）。 */
const GROUP_CATALOG_BATCH_SIZE = 15;
/** 触底判定余量：距滚动底部不足该像素即视为到底。 */
const LOAD_MORE_THRESHOLD_PX = 40;

type GroupCatalogPageProps = {
  scope: 'catalog' | 'mine';
  items: AgentGroupCatalogItem[];
  query: string;
  category: string;
  installation?: 'all' | 'installed' | 'uninstalled';
  status: RequestStatus;
  error: string | null;
  busyIds: ReadonlySet<string>;
  onCategoryChange: (value: string) => void;
  onRetry: () => void;
  onOpen: (id: string) => void;
  onUse: (id: string) => void;
  onInstall: (id: string) => void;
  onCreate: () => void;
};

export function GroupCatalogPage({
  scope,
  items,
  query,
  category,
  installation = 'all',
  status,
  error,
  busyIds,
  onCategoryChange,
  onRetry,
  onOpen,
  onUse,
  onInstall,
  onCreate,
}: GroupCatalogPageProps) {
  const { t } = useTranslation();
  const isMine = scope === 'mine';
  const isEmpty = status === 'success' && items.length === 0;
  const hasQuery = query.trim().length > 0 || Boolean(category) || installation !== 'all';

  const [visibleCount, setVisibleCount] = useState(GROUP_CATALOG_BATCH_SIZE);
  const contentScrollRef = useRef<HTMLDivElement | null>(null);
  const groupSentinelRef = useRef<HTMLDivElement | null>(null);
  const hasMore = visibleCount < items.length;
  const pageItems = items.slice(0, visibleCount);

  // 切换作用域/分类/搜索词后回到首批，对齐专家目录的增量展示行为
  useEffect(() => {
    setVisibleCount(GROUP_CATALOG_BATCH_SIZE);
  }, [items]);

  const appendNextBatch = useCallback(() => {
    setVisibleCount((count) => (count < items.length ? Math.min(count + GROUP_CATALOG_BATCH_SIZE, items.length) : count));
  }, [items.length]);

  // 时间戳节流：滚动/IntersectionObserver 高频触发，100ms 内只允许追加一次。
  const lastAppendAtRef = useRef(0);
  const tryAppend = useCallback(() => {
    const now = performance.now();
    if (now - lastAppendAtRef.current < 100) return;
    lastAppendAtRef.current = now;
    appendNextBatch();
  }, [appendNextBatch]);

  // 滚动触底兜底（IntersectionObserver 为主路径）。
  const handleContentScroll = useCallback(() => {
    if (!hasMore) return;
    const el = contentScrollRef.current;
    if (!el) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - LOAD_MORE_THRESHOLD_PX) tryAppend();
  }, [hasMore, tryAppend]);

  // 哨兵进入视口即追加下一批；rootMargin 提前 LOAD_MORE_THRESHOLD_PX 触发。
  // jsdom 等无布局环境没有 IntersectionObserver，跳过（由 onScroll + 兜底 effect 覆盖）。
  useEffect(() => {
    if (typeof IntersectionObserver === 'undefined') return;
    const root = contentScrollRef.current;
    const sentinel = groupSentinelRef.current;
    if (!root || !sentinel) return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) tryAppend();
      },
      { root, rootMargin: `0px 0px ${LOAD_MORE_THRESHOLD_PX}px 0px` },
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [tryAppend]);

  // 兜底：首批没撑出滚动条时持续追加，直到出现滚动条或加载完，否则后续批次永远加载不出来。
  // clientHeight === 0（无布局，如 jsdom）时跳过，避免误判一次性全量加载。
  useEffect(() => {
    const el = contentScrollRef.current;
    if (!el || !hasMore) return;
    if (el.clientHeight === 0 || el.scrollHeight > el.clientHeight) return;
    appendNextBatch();
  }, [hasMore, appendNextBatch, visibleCount]);

  return (
    <>
      {!isMine ? (
        <div className="page-shell agent-management-toolbar">
          <CategoryTabs
            items={[
              { value: '', label: t('agentManagement.categoryAll') },
              ...GROUP_CATEGORIES.map((item) => ({
                value: item,
                label: t(`agentManagement.categories.${item}`, { defaultValue: item }),
              })),
            ]}
            value={category}
            onChange={onCategoryChange}
            wrapperTestId="agent-group-catalog-category-tabs"
            itemTestId="agent-group-catalog-category-tab"
          />
        </div>
      ) : null}

      <div
        className="page-scroll min-h-0 flex-1 overflow-y-auto"
        data-testid="agent-group-management-catalog-content"
        ref={contentScrollRef}
        onScroll={handleContentScroll}
      >
        {status === 'loading' && items.length === 0 ? (
          // 与专家目录（CatalogPage）同款首屏 loading：此前团队目录首屏是整块空白
          <div
            className="agent-management-state"
            data-testid="agent-group-catalog-loading"
            data-variant="loading"
            role="status"
          >
            <LoadingSpinner size={20} />
            <p>{t('common.loading')}</p>
          </div>
        ) : status === 'error' && items.length === 0 ? (
          // 与 CatalogPage 对齐：刷新失败但仍有旧数据时保留列表，仅首屏无数据才整页错误态
          <div className="agent-management-state agent-management-state--error" role="alert">
            <p>{error || t('agentManagement.group.states.loadError')}</p>
            <button
              type="button"
              className="agent-management-button agent-management-button--secondary"
              data-testid="agent-group-catalog-retry"
              onClick={onRetry}
            >
              {t('common.retry')}
            </button>
          </div>
        ) : isEmpty ? (
          <EmptyState
            id="agent-management-empty-state"
            text={
              hasQuery
                ? t('agentManagement.group.states.noMatch')
                : t(isMine ? 'agentManagement.group.states.mineEmpty' : 'agentManagement.group.states.catalogEmpty')
            }
          >
            {isMine && !hasQuery ? (
              <button
                type="button"
                className="h-[28px] w-[96px] rounded-full border border-[var(--color-button-border)] bg-card text-[12px] text-text"
                data-testid="agent-group-catalog-create-first"
                onClick={onCreate}
              >
                {t('agentManagement.group.actions.createFirst')}
              </button>
            ) : null}
          </EmptyState>
        ) : (
          <>
            <div className="card-grid-auto">
              {pageItems.map((item) => (
                <GroupCard
                  key={item.id}
                  item={item}
                  busy={busyIds.has(item.id)}
                  onOpen={onOpen}
                  onUse={onUse}
                  onInstall={onInstall}
                />
              ))}
            </div>
            {hasMore ? (
              <div
                ref={groupSentinelRef}
                className="flex items-center justify-center gap-2 py-4"
                role="status"
                aria-label={t('agentManagement.loadMore')}
                data-testid="agent-group-catalog-load-more"
              >
                <LoadingSpinner size={16} testId="agent-group-catalog-load-more-spinner" />
                <span className="text-sm text-text-muted">{t('agentManagement.loadMore')}</span>
              </div>
            ) : null}
          </>
        )}
      </div>
    </>
  );
}
