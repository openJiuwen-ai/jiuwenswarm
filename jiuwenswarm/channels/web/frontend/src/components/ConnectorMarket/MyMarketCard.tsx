import { useState } from 'react';
import { Plus, Loader2, AlertCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { AvatarStyle } from '../../utils/skillAvatar';
import { NewConversationIcon } from './icons';
import { PageCard, type PageCardActionProps } from '../ui';
import type { McpCardState } from './mcpState';
import { busyLabelKey } from './mcpState';
import type { McpBusyKind } from '../../types/connector';

interface MyMarketCardProps {
  title: string;
  description: string;
  avatar: AvatarStyle;
  iconUrl?: string;
  state: McpCardState;
  busyKind?: McpBusyKind;
  onOpenDetail: () => void;
  canOpenDetail?: boolean;
  onUse?: () => void;
  onQuickInstall?: () => void;
  quickAction?: 'install' | 'connect';
}

export function MyMarketCard({
  title,
  description,
  avatar,
  iconUrl,
  state,
  busyKind,
  onOpenDetail,
  canOpenDetail = true,
  onUse,
  onQuickInstall,
  quickAction = 'install',
}: MyMarketCardProps) {
  const { t } = useTranslation();
  const [imgFailed, setImgFailed] = useState(false);
  const showUse = state === 'connected' && onUse !== undefined;
  const showInstall = (state === 'idle' || state === 'error') && onQuickInstall !== undefined;
  const showConnecting = state === 'connecting';

  const avatarProp = iconUrl && !imgFailed
    ? <img src={iconUrl} alt="" onError={() => setImgFailed(true)} />
    : avatar;

  const titleEndNode = state === 'error' ? (
    <span
      data-tooltip={t('connectorMarket.card.stateError')}
      className="flex shrink-0 items-center justify-center text-danger"
      title={t('connectorMarket.card.stateError')}
    >
      <AlertCircle size={14} />
    </span>
  ) : undefined;

  let action: PageCardActionProps | undefined;
  let actionSlot: React.ReactNode;

  if (showConnecting) {
    actionSlot = (
      <span className="flex items-center gap-1 text-[12px] text-text-muted">
        <Loader2 size={13} className="animate-spin" />
        {t(busyLabelKey(busyKind))}
      </span>
    );
  } else if (showUse && showInstall) {
    actionSlot = (
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onUse(); }}
          data-tooltip={t('connectorMarket.card.use')}
          className="page-card-action"
        >
          <NewConversationIcon size={14} />
        </button>
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); onQuickInstall(); }}
          data-tooltip={
            state === 'error' ? t('connectorMarket.card.retry') : t(`connectorMarket.card.${quickAction}`)
          }
          className="page-card-action"
        >
          <Plus size={15} strokeWidth={2.5} />
        </button>
      </div>
    );
  } else if (showUse) {
    action = {
      icon: <NewConversationIcon size={14} />,
      onClick: onUse,
      tooltip: t('connectorMarket.card.use'),
    };
  } else if (showInstall) {
    action = {
      icon: <Plus size={15} strokeWidth={2.5} />,
      onClick: onQuickInstall,
      tooltip: state === 'error' ? t('connectorMarket.card.retry') : t(`connectorMarket.card.${quickAction}`),
    };
  }

  return (
    <PageCard
      testId="connector-market-card"
      variant={title}
      onClick={canOpenDetail ? onOpenDetail : undefined}
      avatar={avatarProp}
      title={title}
      titleEnd={titleEndNode}
      description={description}
      action={action}
      actionSlot={actionSlot}
    />
  );
}
