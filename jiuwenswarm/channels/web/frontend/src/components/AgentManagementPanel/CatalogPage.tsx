import { useCallback, useEffect, useRef, useState } from 'react';
import { LoaderCircle } from 'lucide-react';

import { useTranslation } from 'react-i18next';
import { type AgentCatalogItem, type RequestStatus } from '../../features/agentManagement';
import { getAgentAvatarUrl } from '../../features/agentManagement';
import { CategoryTabs, EmptyState, PageCard } from '../ui';
// 深引入而非 ../ui barrel：本组件被 agent-management-layout 测试以 esbuild 独立打包
import { LoadingSpinner } from '../ui/LoadingSpinner/LoadingSpinner';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import ReminderIcon from '../../assets/agent-management/remind.svg?react';

/** 首批渲染数量；触底后每批追加同数量（目录由 listCatalog 一次性载入内存，这里做增量展示）。 */
const CATALOG_BATCH_SIZE = 30;
/** 触底判定余量：距滚动底部不足该像素即视为到底。 */
const LOAD_MORE_THRESHOLD_PX = 40;

const CATEGORIES = [
  'ProductDevelopment',
  'Marketing',
  'Efficiency',
  'DataAnalysis',
  'ContentCreation',
  'SafetyCompliance',
  'Communication',
  'Other',
];

type CatalogPageProps = {
  scope: 'catalog' | 'mine';
  items: AgentCatalogItem[];
  totalItems: number;
  query: string;
  category: string;
  /** 安装态筛选（index.tsx 在进入 view model 前已按它过滤）；非 'all' 时空结果应显示"无匹配"而非"暂无专家" */
  installation?: 'all' | 'installed' | 'uninstalled';
  status: RequestStatus;
  error: string | null;
  busyIds: ReadonlySet<string>;
  onCategoryChange: (value: string) => void;
  onRetry: () => void;
  onOpen: (id: string) => void;
  onUse: (id: string) => void;
  onReconnect: (id: string) => void;
  onInstall: (id: string) => void;
  onCreate: () => void;
};

export function CatalogPage({
  scope,
  items,
  totalItems,
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
  onReconnect,
  onInstall,
  onCreate,
}: CatalogPageProps) {
  const { t } = useTranslation();
  const isMine = scope === 'mine';
  const [visibleCount, setVisibleCount] = useState(CATALOG_BATCH_SIZE);
  const contentScrollRef = useRef<HTMLDivElement | null>(null);
  const catalogSentinelRef = useRef<HTMLDivElement | null>(null);
  const hasMore = visibleCount < items.length;
  const pageItems = items.slice(0, visibleCount);
  const isEmpty = status === 'success' && totalItems === 0;
  const hasQuery = query.trim().length > 0 || Boolean(category) || installation !== 'all';

  // 切换作用域/分类/搜索词/安装态筛选后回到首批（父组件按这些条件重建 items 数组，
  // 依赖 items 即可覆盖全部筛选路径），与 GroupCatalogPage 的重置行为一致
  useEffect(() => {
    setVisibleCount(CATALOG_BATCH_SIZE);
  }, [items]);

  const appendNextBatch = useCallback(() => {
    setVisibleCount((count) => (count < items.length ? Math.min(count + CATALOG_BATCH_SIZE, items.length) : count));
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
    const sentinel = catalogSentinelRef.current;
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
              ...CATEGORIES.map((item) => ({
                value: item,
                label: t(`agentManagement.categories.${item}`, { defaultValue: item }),
              })),
            ]}
            value={category}
            onChange={onCategoryChange}
          />
        </div>
      ) : null}

      <div
        className="page-scroll min-h-0 flex-1 overflow-y-auto"
        data-testid="agent-management-catalog-content"
        ref={contentScrollRef}
        onScroll={handleContentScroll}
      >
        {status === 'loading' && totalItems === 0 ? (
          <div
            className="agent-management-state"
            data-testid="agent-management-catalog-loading"
            data-variant="loading"
            role="status"
          >
            <LoaderCircle className="animate-spin" size={20} aria-hidden="true" />
            <p>{t('common.loading')}</p>
          </div>
        ) : status === 'error' && totalItems === 0 ? (
          <div className="agent-management-state agent-management-state--error" role="alert">
            <p>{error || t('agentManagement.states.loadError')}</p>
            <button
              type="button"
              className="agent-management-button agent-management-button--secondary"
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
                ? t('agentManagement.states.noMatch')
                : t(isMine ? 'agentManagement.states.mineEmpty' : 'agentManagement.states.catalogEmpty')
            }
          >
            {isMine && !hasQuery ? (
              <button
                type="button"
                className="h-[28px] w-[96px] rounded-full border border-[var(--color-button-border)] bg-card text-[12px] text-text"
                onClick={onCreate}
              >
                {t('agentManagement.actions.createFirst')}
              </button>
            ) : null}
          </EmptyState>
        ) : (
          <>
            <div className="card-grid-auto">
              {pageItems.map((item) => {
                const isBusy = busyIds.has(item.id);
                const avatarUrl = getAgentAvatarUrl(item);
                const description = item.description || t('agentManagement.unknownDescription');
                const needsConnection = item.installed && item.connectionState !== 'connected';

                const avatar = { name: item.displayName, iconUrl: avatarUrl, testId: 'agent-management-card-avatar' };

                const labelTags: string[] | undefined = item.tags.length > 0
                  ? item.tags.map(tg => tg.label)
                  : undefined;

                const defaultButton = item.installed
                  ? {
                      text: t('agentManagement.actions.use'),
                      className: 'agent-management-card-action--use',
                      disabled: isBusy || item.enabled === false,
                      onClick: () => {
                        if (needsConnection) onReconnect(item.id);
                        else onUse(item.id);
                      },
                    }
                  : {
                      text: isBusy ? t('agentManagement.actions.installing') : t('agentManagement.actions.install'),
                      disabled: isBusy,
                      busy: isBusy,
                      onClick: () => onInstall(item.id),
                    };

                return (
                  <PageCard
                    key={item.id}
                    className="agent-management-page-card agent-definition-card agent-management-catalog-card"
                    testId="agent-card"
                    variant={item.id}
                    onClick={() => onOpen(item.id)}
                    avatar={avatar}
                    title={item.displayName}
                    titleEnd={
                      scope === 'mine' && item.updateAvailable ? (
                        <UpdateBadge label={t('agentManagement.states.newVersion')} />
                      ) : undefined
                    }
                    label={labelTags}
                    description={description}
                    defaultButton={defaultButton}
                  />
                );
              })}
            </div>
            {hasMore ? (
              <div
                ref={catalogSentinelRef}
                className="flex items-center justify-center gap-2 py-4"
                role="status"
                aria-label={t('agentManagement.loadMore')}
                data-testid="agent-catalog-load-more"
              >
                <LoadingSpinner size={16} testId="agent-catalog-load-more-spinner" />
                <span className="text-sm text-text-muted">{t('agentManagement.loadMore')}</span>
              </div>
            ) : null}
          </>
        )}
      </div>
    </>
  );
}

function UpdateBadge({ label }: { label: string }) {
  const { tooltip, handlers } = useAdaptiveTooltip({ placement: 'top' });
  return (
    <>
      <span
        className="agent-management-card__update"
        data-tooltip={label}
        {...handlers}
      >
        <ReminderIcon aria-hidden="true" />
        <span className="agent-management-card__update-dot" aria-hidden="true" />
      </span>
      {tooltip}
    </>
  );
}
