import { type ReactNode } from 'react';
import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { type AgentCatalogItem, type RequestStatus } from '../../features/agentManagement';
import { getAgentAvatarUrl } from '../../features/agentManagement';
import { CategoryTabs, PageCard } from '../ui';
import ReminderIcon from '../../assets/agent-management/remind.svg?react';

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

function getAvatarLetter(name: string): string {
  return name.trim().slice(0, 1).toUpperCase() || '?';
}

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
              {items.map((item) => {
                const isBusy = busyId === item.id;
                const avatarUrl = getAgentAvatarUrl(item);
                const description = item.description || t('agentManagement.unknownDescription');
                const canUse = item.installed && item.connectionState === 'connected' && item.enabled !== false;
                const needsConnection = item.installed && item.connectionState !== 'connected';

                const avatar = avatarUrl
                  ? <img src={avatarUrl} alt="" />
                  : <span style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', borderRadius: '10px', background: 'var(--color-action-primary-subtle)', color: 'var(--color-text-link)', fontWeight: 700, fontSize: '20px' }}>{getAvatarLetter(item.displayName)}</span>;

                const labelTags: string[] | undefined = item.tags.length > 0
                  ? item.tags.map(tg => tg.label)
                  : (scope === 'mine'
                    ? [t(`agentManagement.categories.${item.category}`, { defaultValue: item.category || t('agentManagement.categoryOther') })]
                    : undefined);

                let actionContent: ReactNode = null;
                if (item.installed) {
                  actionContent = (
                    <div className="agent-management-card__actions" aria-label={t('agentManagement.card.actions', { name: item.displayName })}>
                      <button
                        type="button"
                        className="agent-management-button agent-management-button--secondary agent-management-card-action--use"
                        disabled={!canUse || isBusy}
                        aria-disabled={!canUse}
                        onClick={(e) => { e.stopPropagation(); onUse(item.id); }}
                      >
                        {t('agentManagement.actions.use')}
                      </button>
                      {needsConnection ? (
                        <button
                          type="button"
                          className="agent-management-button agent-management-button--secondary"
                          disabled={isBusy}
                          aria-busy={isBusy}
                          onClick={(e) => { e.stopPropagation(); onReconnect(item.id); }}
                        >
                          {isBusy ? t('agentManagement.actions.connecting') : t('agentManagement.actions.connect')}
                        </button>
                      ) : (
                        <button
                          type="button"
                          className="agent-management-button agent-management-button--primary"
                          disabled={isBusy}
                          aria-busy={isBusy}
                          onClick={(e) => { e.stopPropagation(); onUninstall(item.id); }}
                        >
                          {isBusy ? t('agentManagement.actions.uninstalling') : t('agentManagement.actions.uninstall')}
                        </button>
                      )}
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
                    testId="agent-card"
                    variant={item.id}
                    onClick={() => onOpen(item.id)}
                    avatar={avatar}
                    title={item.displayName}
                    titleEnd={
                      scope === 'mine' && item.updateAvailable ? (
                        <span className="agent-management-card__update">
                          <ReminderIcon aria-hidden="true" />
                          <span className="agent-management-card__update-dot" aria-hidden="true" />
                          <span className="agent-management-card__update-tooltip" role="status">
                            {t('agentManagement.states.newVersion')}
                          </span>
                        </span>
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
