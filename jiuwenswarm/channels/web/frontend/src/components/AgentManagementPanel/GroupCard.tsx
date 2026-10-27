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

export function GroupCard({ item, busy, onOpen, onUse, onInstall }: GroupCardProps) {
  const { t } = useTranslation();
  const canUse = item.installed && item.capabilities.canUse;
  const canInstall = !item.installed && item.capabilities.canInstall;
  const description = item.description || t('agentManagement.unknownDescription');
  const label = item.tags.length > 0 ? item.tags.map((tag) => tag.label) : undefined;
  const avatar = { name: item.displayName, iconUrl: item.avatarUrl, testId: 'agent-management-group-card-avatar' };
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
