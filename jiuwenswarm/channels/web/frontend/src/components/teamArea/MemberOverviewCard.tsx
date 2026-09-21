import type { ReactNode } from 'react';
import { Chevron, formatTime, type ProcessItem } from './shared';

export function MemberOverviewCard({
  memberId,
  displayName,
  sequence,
  statusIcon,
  onClick,
  items,
  emptyText,
}: {
  memberId: string;
  displayName: string;
  sequence: number;
  statusIcon?: ReactNode;
  onClick?: () => void;
  items: ProcessItem[];
  emptyText?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className="w-full rounded-lg border border-border bg-card p-4 text-left hover:border-accent"
      data-testid="member-overview-card"
      data-member-id={memberId}
    >
      <div className="mb-3 flex items-center gap-2">
        {statusIcon}
        <span className="truncate text-sm font-medium text-text">
          {sequence}. {displayName}
        </span>
        <span className="ml-auto text-text-muted">
          <Chevron />
        </span>
      </div>
      {items.length === 0 ? (
        <div className="text-xs text-text-muted">{emptyText}</div>
      ) : (
        <ul className="flex flex-col gap-2">
          {items.slice(0, 6).map((item) => (
            <li key={item.id} className="min-w-0">
              <div className="truncate text-xs text-text">{item.title}</div>
              {item.subtitle ? (
                <div className="truncate text-[11px] text-text-muted">{item.subtitle}</div>
              ) : null}
              {item.timestamp ? (
                <div className="text-[10px] text-text-muted">{formatTime(item.timestamp)}</div>
              ) : null}
            </li>
          ))}
        </ul>
      )}
    </button>
  );
}
