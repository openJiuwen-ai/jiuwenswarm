import { useCallback, useEffect, useRef, useState, type ReactNode } from 'react';
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
  const hasMore = visibleCount < items.length;
  const pageItems = items.slice(0, visibleCount);
  const isEmpty = status === 'success' && totalItems === 0;
  const hasQuery = query.trim().length > 0 || Boolean(category);

  // 切换作用域/分类/搜索词后回到首批，对齐原分页的重置行为
  useEffect(() => {
    setVisibleCount(CATALOG_BATCH_SIZE);
  }, [scope, query, category]);

  const appendNextBatch = useCallback(() => {
    setVisibleCount((count) => (count < items.length ? Math.min(count + CATALOG_BATCH_SIZE, items.length) : count));
  }, [items.length]);

  // 滚动触底：底部加载组件已可见，追加下一批（数据在内存中，追加为同步展示）
  const handleContentScroll = useCallback(() => {
    const el = contentScrollRef.current;
    if (!el || !hasMore) return;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - LOAD_MORE_THRESHOLD_PX) appendNextBatch();
  }, [appendNextBatch, hasMore]);

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

                let actionContent: ReactNode = null;
                if (item.installed) {
                  actionContent = (
                    <div className="agent-management-card__actions" aria-label={t('agentManagement.card.actions', { name: item.displayName })}>
                      <button
                        type="button"
                        className="agent-management-button agent-management-button--primary agent-management-card-action--use"
                        disabled={isBusy || item.enabled === false}
                        aria-disabled={isBusy || item.enabled === false}
                        onClick={(e) => { e.stopPropagation(); needsConnection ? onReconnect(item.id) : onUse(item.id); }}
                      >
                        {t('agentManagement.actions.use')}
                      </button>

                    </div>
                  );
                } else {
                  actionContent = (
                    <div className="agent-management-card__actions" aria-label={t('agentManagement.card.actions', { name: item.displayName })}>
                      <button
                        type="button"
                        className="agent-management-button agent-management-button--primary"
                        disabled={isBusy}
                        aria-busy={isBusy}
                        onClick={(e) => { e.stopPropagation(); onInstall(item.id); }}
                      >
                        {isBusy ? t('agentManagement.actions.installing') : t('agentManagement.actions.install')}
                      </button>
                    </div>
                  );
                }

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
                    actionSlot={actionContent}
                  />
                );
              })}
            </div>
            {hasMore ? (
              <div
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
