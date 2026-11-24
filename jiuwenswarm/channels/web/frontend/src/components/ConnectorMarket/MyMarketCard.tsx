import { MarketCard } from './MarketCard';
import type { McpCardState } from './mcpState';
import type { McpBusyKind } from '../../types/connector';
import type { ReactNode } from 'react';

interface MyMarketCardProps {
  title: string;
  tags?: string[];
  description: string;
  iconUrl?: string;
  state: McpCardState;
  busyKind?: McpBusyKind;
  onOpenDetail: () => void;
  canOpenDetail?: boolean;
  onUse?: () => void;
  onQuickInstall?: () => void;
  quickAction?: 'install' | 'connect';
  actionMode?: 'auto' | 'use';
  actionDisabled?: boolean;
  connectedLabel?: string;
  onConnectedAction?: () => void;
  titleEnd?: ReactNode;
  headerAction?: ReactNode;
  hideConnectedAction?: boolean;
  onCancelConnect?: () => void;
}

export function MyMarketCard({
  title,
  tags = [],
  description,
  iconUrl,
  state,
  busyKind,
  onOpenDetail,
  canOpenDetail = true,
  onUse,
  onQuickInstall,
  quickAction = 'install',
  actionMode = 'auto',
  actionDisabled,
  connectedLabel,
  onConnectedAction,
  titleEnd,
  headerAction,
  hideConnectedAction,
  onCancelConnect,
}: MyMarketCardProps) {
  return (
    <MarketCard
      title={title}
      tags={tags}
      description={description}
      iconUrl={iconUrl}
      state={state}
      busyKind={busyKind}
      canOpenDetail={canOpenDetail}
      onOpenDetail={onOpenDetail}
      onUse={onUse}
      onQuickAdd={() => onQuickInstall?.()}
      quickAction={quickAction}
      actionMode={actionMode}
      actionDisabled={actionDisabled}
      onCancelConnect={onCancelConnect}
      connectedLabel={connectedLabel}
      onConnectedAction={onConnectedAction}
      titleEnd={titleEnd}
      headerAction={headerAction}
      hideConnectedAction={hideConnectedAction}
    />
  );
}
