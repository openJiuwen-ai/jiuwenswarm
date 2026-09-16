import { useState, type ReactNode } from 'react';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { ProcessListCard } from './ProcessListCard';
import type { ProcessItem } from './shared';

type MemberOverviewCardProps = {
  memberId: string;
  displayName: string;
  sequence: number;
  statusIcon?: ReactNode;
  onClick?: () => void;
  items: ProcessItem[];
  emptyText?: string;
};

export function MemberOverviewCard({
  memberId,
  displayName,
  sequence,
  statusIcon,
  onClick,
  items,
  emptyText,
}: MemberOverviewCardProps) {
  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  const toggleItem = (itemId: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  };

  return (
    <div
      data-testid="team-area-member-overview-card"
      data-variant={memberId}
      className="relative flex h-[240px] flex-col gap-3 overflow-hidden rounded-[8px] border-[1.5px] border-border bg-card p-4 text-left hover:border-[var(--color-action-primary)] transition-colors"
    >
      <button
        type="button"
        onClick={onClick}
        className="flex items-center gap-3 text-left cursor-pointer"
        data-testid="team-area-member-overview-card-header"
      >
        <span
          className="absolute left-0 top-0 flex h-[18px] w-[18px] items-center justify-center text-[12px] leading-[18px] text-text bg-[var(--color-member-card-badge-surface)] rounded-tl-[4px] rounded-br-[8px] rounded-tr-none rounded-bl-none"
          data-testid="team-area-member-overview-card-sequence"
        >
          {sequence}
        </span>
        <div className="relative shrink-0">
          <TeamMemberAvatar
            member={memberId}
            alt={displayName}
            className="h-8 w-8 rounded-full"
            imageClassName="rounded-full"
          />
        </div>
        <div className="min-w-0 flex-1">
          <div className="truncate text-sm font-normal text-text" data-testid="team-area-member-overview-card-name">
            {displayName}
          </div>
          <div className="mt-0.5 truncate text-xs text-text-muted" data-testid="team-area-member-overview-card-id">
            @{memberId}
          </div>
        </div>
        {statusIcon}
      </button>
      <div className="flex min-w-0 min-h-0 flex-1 flex-col -mr-4 [container-type:inline-size]">
        <div className="min-w-0 min-h-0 flex-1 overflow-y-auto">
          <div className="flex min-h-full w-[calc(100cqw_-_1rem)] flex-col">
            <ProcessListCard items={items} expandedIds={expandedIds} onToggle={toggleItem} emptyText={emptyText} />
          </div>
        </div>
      </div>
    </div>
  );
}
