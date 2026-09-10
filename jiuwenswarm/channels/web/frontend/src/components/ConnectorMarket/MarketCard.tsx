import { useState } from 'react';
import { Plus, Loader2, AlertCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { AvatarStyle } from '../../utils/skillAvatar';
import { NewConversationIcon } from './icons';
import { PageCard, type PageCardActionProps } from '../ui';
import type { McpCardState } from './mcpState';
import { busyLabelKey } from './mcpState';
import type { McpBusyKind } from '../../types/connector';

interface MarketCardProps {
  title: string;
  description: string;
  avatar: AvatarStyle;
  iconUrl?: string;
  state: McpCardState;
  busyKind?: McpBusyKind;
  canOpenDetail: boolean;
  onOpenDetail: () => void;
  onQuickAdd: () => void;
  quickAction?: 'install' | 'connect';
  onUse?: () => void;
}

export function MarketCard({
  title,
  description,
  avatar,
  iconUrl,
  state,
  busyKind,
  canOpenDetail,
  onOpenDetail,
  onQuickAdd,
  quickAction = 'install',
  onUse,
}: MarketCardProps) {
  const { t } = useTranslation();
  const [imgFailed, setImgFailed] = useState(false);

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

  // 单按钮统一走 PageCard action（page-card-action 32×32 统一样式）；
  // connecting 是非按钮内容（spinner + 文案），必须走 actionSlot。
  let action: PageCardActionProps | undefined;
  let actionSlot: React.ReactNode;

  if (state === 'connecting') {
    actionSlot = (
      <span className="flex items-center gap-1 text-[12px] text-text-muted">
        <Loader2 size={13} className="animate-spin" />
        {t(busyLabelKey(busyKind))}
      </span>
    );
  } else if (state === 'connected') {
    action = {
      icon: <NewConversationIcon size={15} />,
      onClick: onUse,
      tooltip: t('connectorMarket.card.use'),
    };
  } else if (state === 'idle' || state === 'error') {
    action = {
      icon: <Plus size={16} strokeWidth={2.5} />,
      onClick: onQuickAdd,
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
