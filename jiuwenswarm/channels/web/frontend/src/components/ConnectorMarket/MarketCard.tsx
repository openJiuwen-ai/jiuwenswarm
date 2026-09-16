import './ConnectorMarket.css';
import { useState } from 'react';
import { Loader2, AlertCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { AvatarStyle } from '../../utils/skillAvatar';
import { PageCard } from '../ui';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import type { McpCardState } from './mcpState';
import { busyLabelKey } from './mcpState';
import type { McpBusyKind } from '../../types/connector';

interface MarketCardProps {
  title: string;
  tags?: string[];
  description: string;
  avatar: AvatarStyle;
  iconUrl?: string;
  state: McpCardState;
  busyKind?: McpBusyKind;
  canOpenDetail: boolean;
  onOpenDetail: () => void;
  onQuickAdd: () => void;
  quickAction?: 'install' | 'connect';
  actionDisabled?: boolean;
  onUse?: () => void;
}

export function MarketCard({
  title,
  tags = [],
  description,
  avatar,
  iconUrl,
  state,
  busyKind,
  canOpenDetail,
  onOpenDetail,
  onQuickAdd,
  quickAction = 'install',
  actionDisabled = false,
  onUse,
}: MarketCardProps) {
  const { t } = useTranslation();
  const [imgFailed, setImgFailed] = useState(false);
  const { tooltip: errorTooltip, handlers: errorTooltipHandlers } = useAdaptiveTooltip({ placement: 'top' });

  const avatarProp = iconUrl && !imgFailed
    ? <img src={iconUrl} alt="" onError={() => setImgFailed(true)} />
    : avatar;

  const titleEndNode = state === 'error' ? (
    <>
      <span
        data-tooltip={t('connectorMarket.card.stateError')}
        className="flex shrink-0 items-center justify-center text-danger"
        {...errorTooltipHandlers}
      >
        <AlertCircle size={14} />
      </span>
      {errorTooltip}
    </>
  ) : undefined;

  // 安装操作使用与专家卡片一致的文字按钮，已连接仍保留会话入口。
  let actionSlot: React.ReactNode;

  if (state === 'connecting') {
    actionSlot = (
      <span className="flex items-center gap-1 text-[12px] text-text-muted">
        <Loader2 size={13} className="animate-spin" />
        {t(busyLabelKey(busyKind))}
      </span>
    );
  } else if (state === 'connected') {
    actionSlot = <button type="button" className="connector-market-card-install" data-testid="connector-market-card-use" disabled={!onUse} onClick={event => { event.stopPropagation(); onUse?.(); }}>{t('connectorMarket.card.use')}</button>;
  } else if (state === 'idle' || state === 'error') {
    actionSlot = (
      <button
        type="button"
        className="connector-market-card-install"
        disabled={actionDisabled}
        data-testid="connector-market-card-install"
        onClick={(event) => {
          event.stopPropagation();
          onQuickAdd();
        }}
      >
        {state === 'error' ? t('connectorMarket.card.retry') : t(`connectorMarket.card.${quickAction}`)}
      </button>
    );
  }

  return (
    <PageCard
      testId="connector-market-card"
      variant={title}
      onClick={canOpenDetail ? onOpenDetail : undefined}
      avatar={avatarProp}
      title={title}
      label={tags}
      titleEnd={titleEndNode}
      description={description}
      actionSlot={actionSlot}
    />
  );
}
