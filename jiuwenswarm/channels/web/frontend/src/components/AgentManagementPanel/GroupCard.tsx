import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { AgentGroupCatalogItem } from '../../features/agentManagement';
import { PageCard } from '../ui';

type GroupCardProps = {
  item: AgentGroupCatalogItem;
  busy: boolean;
  onOpen: (id: string) => void;
  onUse: (id: string) => void;
  onInstall: (id: string) => void;
  onUninstall: (id: string) => void;
};

export function getAvatarTone(name: string): string {
  const seed = Array.from(name.trim() || '?').reduce((total, char) => total + char.charCodeAt(0), 0);
  return ['variant-1', 'variant-2', 'variant-3', 'variant-4', 'variant-5', 'variant-6'][seed % 6];
}

function GroupAvatar({
  item,
  size = 'card',
}: {
  item: Pick<AgentGroupCatalogItem, 'displayName' | 'avatarUrl'>;
  size?: 'card' | 'detail';
}) {
  const [imageFailed, setImageFailed] = useState(false);
  const avatarUrl = item.avatarUrl && !imageFailed ? item.avatarUrl : null;
  return (
    <span
      className={`agent-management-avatar agent-group-avatar agent-group-avatar--${size} agent-group-avatar--${getAvatarTone(item.displayName)}`}
      aria-hidden="true"
    >
      {avatarUrl ? (
        <img src={avatarUrl} alt="" onError={() => setImageFailed(true)} />
      ) : (
        <span>{item.displayName.trim().slice(0, 1).toUpperCase() || '?'}</span>
      )}
    </span>
  );
}

export function GroupCard({ item, busy, onOpen, onUse, onInstall, onUninstall }: GroupCardProps) {
  const { t } = useTranslation();
  const canUse = item.installed && item.capabilities.canUse;
  const canInstall = !item.installed && item.capabilities.canInstall;
  const canUninstall = item.capabilities.canUninstall;
  const fallbackTag = t(`agentManagement.categories.${item.category}`, {
    defaultValue: item.category || t('agentManagement.categoryOther'),
  });
  const description = item.description || t('agentManagement.unknownDescription');
  const label = item.tags.length > 0 ? item.tags.map((tag) => tag.label) : [fallbackTag];
  const avatar = <GroupAvatar item={item} />;
  const actionContent = (
    <div
      className="agent-management-card__actions"
      aria-label={t('agentManagement.group.card.actions', { name: item.displayName })}
      data-testid="agent-group-card-actions"
      onClick={(event) => event.stopPropagation()}
    >
      {canUse ? (
        <button
          type="button"
          className="agent-management-button agent-management-button--secondary agent-management-card-action--use"
          data-testid="agent-group-card-action"
          data-variant="use"
          disabled={busy}
          aria-disabled={!canUse}
          onClick={(event) => {
            event.stopPropagation();
            onUse(item.id);
          }}
        >
          {t('agentManagement.group.actions.use')}
        </button>
      ) : null}
      {canInstall ? (
        <button
          type="button"
          className="agent-management-button agent-management-button--primary"
          data-testid="agent-group-card-action"
          data-variant="install"
          disabled={busy}
          aria-busy={busy}
          onClick={(event) => {
            event.stopPropagation();
            onInstall(item.id);
          }}
        >
          {busy ? t('agentManagement.group.actions.installing') : t('agentManagement.group.actions.install')}
        </button>
      ) : canUninstall ? (
        <button
          type="button"
          className="agent-management-button agent-management-button--primary"
          data-testid="agent-group-card-action"
          data-variant={item.installed ? 'uninstall' : 'delete'}
          disabled={busy}
          aria-busy={busy}
          onClick={(event) => {
            event.stopPropagation();
            onUninstall(item.id);
          }}
        >
          {busy
            ? t('agentManagement.group.actions.uninstalling')
            : t(item.installed ? 'agentManagement.group.actions.uninstall' : 'agentManagement.group.actions.delete')}
        </button>
      ) : null}
    </div>
  );

  return (
    <PageCard
      className="agent-management-page-card agent-group-card"
      testId={`agent-group-card-${item.id}`}
      headerTestId="agent-group-card-open"
      variant={item.id}
      onClick={() => onOpen(item.id)}
      interactive
      ariaLabel={t('agentManagement.group.card.open', { name: item.displayName })}
      avatar={avatar}
      title={item.displayName}
      label={label}
      description={description}
      actionSlot={actionContent}
    />
  );
}

export { GroupAvatar };
