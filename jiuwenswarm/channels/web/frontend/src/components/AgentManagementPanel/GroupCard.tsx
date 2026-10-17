import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { AgentGroupCatalogItem } from '../../features/agentManagement';
import { PageCard, type PageCardDefaultButton } from '../ui';

type GroupCardProps = {
  item: AgentGroupCatalogItem;
  busy: boolean;
  onOpen: (id: string) => void;
  onUse: (id: string) => void;
  onInstall: (id: string) => void;
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

export function GroupCard({ item, busy, onOpen, onUse, onInstall }: GroupCardProps) {
  const { t } = useTranslation();
  const canUse = item.installed && item.capabilities.canUse;
  const canInstall = !item.installed && item.capabilities.canInstall;
  const description = item.description || t('agentManagement.unknownDescription');
  const label = item.tags.length > 0 ? item.tags.map((tag) => tag.label) : undefined;
  const avatar = <GroupAvatar item={item} />;
  const defaultButton: PageCardDefaultButton | undefined = canUse
    ? {
        text: t('agentManagement.group.actions.use'),
        testId: 'agent-group-card-action',
        variant: 'use',
        disabled: busy,
        className: 'agent-management-card-action--use',
        onClick: () => onUse(item.id),
      }
    : canInstall
      ? {
          text: busy ? t('agentManagement.group.actions.installing') : t('agentManagement.group.actions.install'),
          testId: 'agent-group-card-action',
          variant: 'install',
          disabled: busy,
          busy,
          onClick: () => onInstall(item.id),
        }
      : undefined;

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
      defaultButton={defaultButton}
    />
  );
}

export { GroupAvatar };
