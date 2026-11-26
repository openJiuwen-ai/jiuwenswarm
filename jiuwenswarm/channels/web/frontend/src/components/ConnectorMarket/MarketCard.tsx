import './ConnectorMarket.css';
import type { ReactNode } from 'react';
import { Loader2, AlertCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { PageCard } from '../ui';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import type { McpCardState } from './mcpState';
import { busyLabelKey, canCancelConnect } from './mcpState';
import type { McpBusyKind } from '../../types/connector';

interface MarketCardProps {
  title: string;
  tags?: string[];
  description: string;
  iconUrl?: string;
  state: McpCardState;
  busyKind?: McpBusyKind;
  canOpenDetail: boolean;
  onOpenDetail: () => void;
  onQuickAdd: () => void;
  quickAction?: 'install' | 'connect' | 'update';
  actionMode?: 'auto' | 'use';
  actionDisabled?: boolean;
  onUse?: () => void;
  /** 错误态红点的提示文案；不传为"连接失败"。 */
  errorLabel?: string;
  /** 覆盖已连接态按钮文案；系统插件用它表达"已启用/重载"。 */
  connectedLabel?: string;
  /** 已连接态按钮动作；缺省沿用 onUse。 */
  onConnectedAction?: () => void;
  /** Optional control rendered beside the title, for package-level switches. */
  titleEnd?: ReactNode;
  /** Optional control rendered in the card header action area. */
  headerAction?: ReactNode;
  /** Keep an installed card navigable without adding a second action button. */
  hideConnectedAction?: boolean;
  /** Cancel entry while the backend is still connecting; omitted for local busy kinds. */
  onCancelConnect?: () => void;
}

export function MarketCard({
  title,
  tags = [],
  description,
  iconUrl,
  state,
  busyKind,
  canOpenDetail,
  onOpenDetail,
  onQuickAdd,
  quickAction = 'install',
  actionMode = 'auto',
  actionDisabled = false,
  onUse,
  errorLabel,
  onCancelConnect,
  connectedLabel,
  onConnectedAction,
  titleEnd,
  headerAction,
  hideConnectedAction = false,
}: MarketCardProps) {
  const { t } = useTranslation();
  const { tooltip: errorTooltip, handlers: errorTooltipHandlers } = useAdaptiveTooltip({ placement: 'top' });

  const avatarProp = { name: title, iconUrl, testId: 'connector-market-card-avatar' };

  const titleEndNode = (
    <>
      {state === 'error' && (
        <span
          data-tooltip={errorLabel ?? t('connectorMarket.card.stateError')}
          className="flex shrink-0 items-center justify-center text-danger"
          {...errorTooltipHandlers}
        >
          <AlertCircle size={14} />
        </span>
      )}
      {titleEnd}
      {errorTooltip}
    </>
  );

  const headerActionNode = headerAction ? (
    <span onClick={(event) => event.stopPropagation()}>{headerAction}</span>
  ) : null;

  // 安装操作使用与专家卡片一致的文字按钮，已连接仍保留会话入口。
  let actionSlot: React.ReactNode;

  if (state === 'connecting') {
    const cancellable = onCancelConnect && canCancelConnect(busyKind);
    actionSlot = (
      <span className="flex items-center gap-1 text-[12px] text-text-muted">
        <Loader2 size={13} className="animate-spin" />
        {t(busyLabelKey(busyKind))}
        {cancellable && (
          <button
            type="button"
            className="font-medium text-text-link hover:opacity-80"
            data-testid="connector-market-card-cancel-connect"
            onClick={(event) => {
              event.stopPropagation();
              onCancelConnect?.();
            }}
          >
            {t('connectorMarket.cliAuth.cancelConnect')}
          </button>
        )}
      </span>
    );
  } else if ((state === 'connected' || actionMode === 'use') && (onConnectedAction ?? onUse)) {
    // Keep use/session entry; when hasUpdate, show Update beside it (do not replace).
    const action = onConnectedAction ?? onUse;
    const showUpdate = quickAction === 'update';
    const useButton = hideConnectedAction ? null : (
      <button
        type="button"
        className="connector-market-card-install"
        data-testid="connector-market-card-use"
        disabled={actionDisabled}
        onClick={event => { event.stopPropagation(); action?.(); }}
      >
        {connectedLabel ?? t('connectorMarket.card.use')}
      </button>
    );
    const updateButton = showUpdate ? (
      <button
        type="button"
        className="connector-market-card-install"
        disabled={actionDisabled}
        data-testid="connector-market-card-update"
        onClick={(event) => {
          event.stopPropagation();
          onQuickAdd();
        }}
      >
        {t('connectorMarket.card.update')}
      </button>
    ) : null;
    actionSlot =
      useButton || updateButton ? (
        <span className="flex items-center gap-2">
          {useButton}
          {updateButton}
        </span>
      ) : null;
  } else if (quickAction === 'update' && (state === 'idle' || state === 'error')) {
    // Installed but not in a use/session slot — Update only (mcp.update still placeholder).
    actionSlot = (
      <button
        type="button"
        className="connector-market-card-install"
        disabled={actionDisabled}
        data-testid="connector-market-card-update"
        onClick={(event) => {
          event.stopPropagation();
          onQuickAdd();
        }}
      >
        {t('connectorMarket.card.update')}
      </button>
    );
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
      actionSlot={headerActionNode ? <>{actionSlot}{headerActionNode}</> : actionSlot}
    />
  );
}
