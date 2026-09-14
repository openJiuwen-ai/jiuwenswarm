import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { AgentGroupCatalogItem, RequestStatus } from '../../features/agentManagement';
import { CategoryTabs } from '../ui';
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

const PAGE_SIZE = 15;

type GroupCatalogPageProps = {
  scope: 'catalog' | 'mine';
  items: AgentGroupCatalogItem[];
  totalItems: number;
  page: number;
  totalPages: number;
  query: string;
  category: string;
  status: RequestStatus;
  error: string | null;
  busyId: string | null;
  onCategoryChange: (value: string) => void;
  onPageChange: (page: number) => void;
  onRetry: () => void;
  onOpen: (id: string) => void;
  onUse: (id: string) => void;
  onInstall: (id: string) => void;
  onUninstall: (id: string) => void;
  onCreate: () => void;
};

export function GroupCatalogPage({
  scope,
  items,
  totalItems,
  page,
  totalPages,
  query,
  category,
  status,
  error,
  busyId,
  onCategoryChange,
  onPageChange,
  onRetry,
  onOpen,
  onUse,
  onInstall,
  onUninstall,
  onCreate,
}: GroupCatalogPageProps) {
  const { t } = useTranslation();
  const isMine = scope === 'mine';
  const isEmpty = status === 'success' && totalItems === 0;
  const hasQuery = query.trim().length > 0 || Boolean(category);

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

      <div className="page-scroll min-h-0 flex-1 overflow-y-auto" data-testid="agent-group-management-catalog-content">
        {status === 'loading' && totalItems === 0 ? null : status === 'error' ? (
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
          <div className="agent-management-state" data-testid="agent-management-empty-state" data-kind="group">
            <p>
              {hasQuery
                ? t('agentManagement.group.states.noMatch')
                : t(isMine ? 'agentManagement.group.states.mineEmpty' : 'agentManagement.group.states.catalogEmpty')}
            </p>
            {isMine && !hasQuery ? (
              <button
                type="button"
                className="agent-management-button agent-management-button--primary"
                data-testid="agent-group-catalog-create-first"
                onClick={onCreate}
              >
                {t('agentManagement.group.actions.createFirst')}
              </button>
            ) : null}
          </div>
        ) : (
          <>
            <div className="card-grid-auto">
              {items.map((item) => (
                <GroupCard
                  key={item.id}
                  item={item}
                  busy={busyId === item.id}
                  onOpen={onOpen}
                  onUse={onUse}
                  onInstall={onInstall}
                  onUninstall={onUninstall}
                />
              ))}
            </div>
            {totalPages > 1 ? (
              <div
                className="agent-management-pagination"
                aria-label={t('agentManagement.pagination.label')}
                data-testid="agent-group-catalog-pagination"
              >
                <span>
                  {t('agentManagement.pagination.range', {
                    start: (page - 1) * PAGE_SIZE + 1,
                    end: Math.min(page * PAGE_SIZE, totalItems),
                    total: totalItems,
                  })}
                </span>
                <div className="agent-management-pagination__buttons">
                  <button
                    type="button"
                    data-testid="agent-group-catalog-page-previous"
                    disabled={page <= 1}
                    onClick={() => onPageChange(page - 1)}
                    aria-label={t('agentManagement.pagination.previous')}
                  >
                    <ChevronLeft size={16} aria-hidden="true" />
                  </button>
                  <span>{t('agentManagement.pagination.page', { page, total: totalPages })}</span>
                  <button
                    type="button"
                    data-testid="agent-group-catalog-page-next"
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

export { PAGE_SIZE as GROUP_PAGE_SIZE };
