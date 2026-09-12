import { type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { type AgentCatalogItem, type RequestStatus } from '../../features/agentManagement';
import { getAgentAvatarUrl } from '../../features/agentManagement';
import { CategoryTabs, PageCard } from '../ui';
import { getSkillAvatar } from '../../utils/skillAvatar';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import ReminderIcon from '../../assets/agent-management/remind.svg?react';

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
  busyId: string | null;
  onCategoryChange: (value: string) => void;
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
                  : getSkillAvatar(item.displayName);

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
