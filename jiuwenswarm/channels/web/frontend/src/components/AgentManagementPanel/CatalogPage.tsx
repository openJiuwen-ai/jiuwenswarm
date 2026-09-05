import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { AgentCatalogItem, RequestStatus } from '../../features/agentManagement';
import { CategoryTabs } from '../ui';
import { DefinitionCard } from './DefinitionCard';

const PAGE_SIZE = 15;
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
  onReconnect: (id: string) => void;
  onInstall: (id: string) => void;
  onUninstall: (id: string) => void;
  onCreate: () => void;
};

export function CatalogPage({
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
  onReconnect,
  onInstall,
  onUninstall,
  onCreate,
}: CatalogPageProps) {
  const { t } = useTranslation();
  const isMine = scope === 'mine';
  const isEmpty = status === 'success' && totalItems === 0;
  const hasQuery = query.trim().length > 0 || Boolean(category);

  return (
    <>
      {!isMine ? (
        <div className="agent-management-toolbar">
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

      <div className="min-h-0 flex-1 overflow-y-auto" data-testid="agent-management-catalog-content">
        {status === 'loading' && totalItems === 0 ? null : status === 'error' ? (
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
          <div className="agent-management-state">
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
            <div className="card-grid-auto" style={{ paddingTop: '16px' }}>
              {items.map((item) => (
                <DefinitionCard
                  key={item.id}
                  item={item}
                  scope={scope}
                  busy={busyId === item.id}
                  onOpen={onOpen}
                  onUse={onUse}
                  onReconnect={onReconnect}
                  onInstall={onInstall}
                  onUninstall={onUninstall}
                />
              ))}
            </div>
            {totalPages > 1 ? (
              <div className="agent-management-pagination" aria-label={t('agentManagement.pagination.label')}>
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
                    disabled={page <= 1}
                    onClick={() => onPageChange(page - 1)}
                    aria-label={t('agentManagement.pagination.previous')}
                  >
                    <ChevronLeft size={16} aria-hidden="true" />
                  </button>
                  <span>{t('agentManagement.pagination.page', { page, total: totalPages })}</span>
                  <button
                    type="button"
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

export { PAGE_SIZE };
