import { MarketCard } from './MarketCard';
import type { McpCardState } from './mcpState';
import type { McpBusyKind } from '../../types/connector';

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
  actionDisabled?: boolean;
  connectedLabel?: string;
  onConnectedAction?: () => void;
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
  actionDisabled,
  connectedLabel,
  onConnectedAction,
}: MyMarketCardProps) {
  return <MarketCard title={title} tags={tags} description={description} iconUrl={iconUrl} state={state} busyKind={busyKind} canOpenDetail={canOpenDetail} onOpenDetail={onOpenDetail} onUse={onUse} onQuickAdd={() => onQuickInstall?.()} quickAction={quickAction} actionDisabled={actionDisabled} connectedLabel={connectedLabel} onConnectedAction={onConnectedAction} />;
}
