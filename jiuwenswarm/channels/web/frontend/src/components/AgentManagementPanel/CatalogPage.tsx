import { type ReactNode } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';

import { useTranslation } from 'react-i18next';
import { type AgentCatalogItem, type RequestStatus } from '../../features/agentManagement';
import { getAgentAvatarUrl } from '../../features/agentManagement';
import { CategoryTabs, PageCard } from '../ui';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import ReminderIcon from '../../assets/agent-management/remind.svg?react';

const AGENT_PAGE_SIZE = 15;

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
  page: number;
  onPageChange: (page: number) => void;
  query: string;
  category: string;
  status: RequestStatus;
  error: string | null;
  busyId: string | null;
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
  page: requestedPage,
  onPageChange,
  query,
  category,
  status,
  error,
  busyId,
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
  const totalPages = Math.max(1, Math.ceil(totalItems / AGENT_PAGE_SIZE));
  const page = Math.min(Math.max(1, requestedPage), totalPages);
  const pageItems = items.slice((page - 1) * AGENT_PAGE_SIZE, page * AGENT_PAGE_SIZE);
  const isEmpty = status === 'success' && totalItems === 0;
  const hasQuery = query.trim().length > 0 || Boolean(category);

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

      <div className="page-scroll min-h-0 flex-1 overflow-y-auto" data-testid="agent-management-catalog-content">
        {status === 'loading' && totalItems === 0 ? null : status === 'error' && totalItems === 0 ? (
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
          <div className="agent-management-state" data-testid="agent-management-empty-state" data-kind="agent">
            <p>
              {hasQuery
                ? t('agentManagement.states.noMatch')
                : t(isMine ? 'agentManagement.states.mineEmpty' : 'agentManagement.states.catalogEmpty')}
            </p>
            {isMine && !hasQuery ? (
              <button
                type="button"
                className="agent-management-button agent-management-button--primary"
                onClick={onCreate}
              >
                {t('agentManagement.actions.createFirst')}
              </button>
            ) : null}
          </div>
        ) : (
          <>
            <div className="card-grid-auto">
              {pageItems.map((item) => {
                const isBusy = busyId === item.id;
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
            {totalPages > 1 ? (
              <div
                className="agent-management-pagination"
                aria-label={t('agentManagement.pagination.label')}
                data-testid="agent-catalog-pagination"
              >
                <span>
                  {t('agentManagement.pagination.range', {
                    start: (page - 1) * AGENT_PAGE_SIZE + 1,
                    end: Math.min(page * AGENT_PAGE_SIZE, totalItems),
                    total: totalItems,
                  })}
                </span>
                <div className="agent-management-pagination__buttons">
                  <button
                    type="button"
                    data-testid="agent-catalog-page-previous"
                    disabled={page <= 1}
                    onClick={() => onPageChange(page - 1)}
                    aria-label={t('agentManagement.pagination.previous')}
                  >
                    <ChevronLeft size={16} aria-hidden="true" />
                  </button>
                  <span>{t('agentManagement.pagination.page', { page, total: totalPages })}</span>
                  <button
                    type="button"
                    data-testid="agent-catalog-page-next"
                    disabled={page >= totalPages}
                    onClick={() => onPageChange(page + 1)}
                    aria-label={t('agentManagement.pagination.next')}
                  >
                    <ChevronRight size={16} aria-hidden="true" />
                  </button>
                </div>
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
