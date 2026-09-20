import { TeamMemberAvatar } from '../TeamMemberAvatar';
import PendingIcon from '../../assets/pending.svg?react';
import {
  getMemberPlainName,
  getMemberStatusKey,
  type TeamMember,
} from './shared';

interface TaskProgress {
  completed: number;
  total: number;
}

export function MemberListItem({
  member,
  selected,
  compact,
  onClick,
  taskProgress,
}: {
  member: TeamMember;
  selected?: boolean;
  compact?: boolean;
  onClick?: () => void;
  taskProgress?: TaskProgress;
}) {
  const displayName = getMemberPlainName(member);
  const statusKey = getMemberStatusKey(member);

  const progressPercent = taskProgress && taskProgress.total > 0
    ? Math.round((taskProgress.completed / taskProgress.total) * 100)
    : 0;
  const radius = 14;
  const strokeWidth = 2;
  const circumference = 2 * Math.PI * radius;
  const strokeDashoffset = circumference - (progressPercent / 100) * circumference;

  const isRunning = statusKey === 'running';

  return (
    <button
      type="button"
      onClick={onClick}
      data-testid="team-area-member-item"
      data-variant={member.member_id}
      className={`flex w-full items-center gap-3 rounded-md text-left  ${
        compact ? 'p-2' : 'px-[8px] py-[9px]'
      } ${
        selected
          ? 'border border-transparent bg-[var(--color-tool-tab-active-bg)]'
          : 'border border-transparent hover:bg-secondary'
      }`}
    >
      <div className="relative shrink-0" data-testid="team-area-member-item-avatar">
        <TeamMemberAvatar
          member={member.member_id}
          alt={displayName}
          className="h-8 w-8 rounded-full"
          imageClassName="rounded-full"
        />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span
            className={`${compact ? 'text-xs' : 'text-sm'} truncate font-semibold text-text`}
            data-testid="team-area-member-item-name"
          >
            {displayName}
          </span>
        </div>
        {/* 第二行固定给 member_id：display name 由 leader 起，同队重名很常见
            （三个"通用协作专员"），而 @ 时要敲的正是 id。形态与输入框的 @ 下拉
            一致，两处对得上。主行因此用不消歧的纯展示名，避免和这里重复。 */}
        <div className="mt-0.5 truncate text-xs text-text-muted" data-testid="team-area-member-item-id">
          @{member.member_id}
        </div>
      </div>
      {compact ? (
        isRunning ? (
          <svg className="w-4 h-4 text-info animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 2v4" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m16.2 7.8 2.9-2.9" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M18 12h4" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m16.2 16.2 2.9 2.9" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 18v4" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m4.9 19.1 2.9-2.9" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2 12h4" />
            <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m4.9 4.9 2.9 2.9" />
          </svg>
        ) : (
          <PendingIcon className="w-4 h-4 text-text-muted" />
        )
      ) : taskProgress && taskProgress.total > 0 ? (
        <div className="shrink-0 relative">
          <svg width="32" height="32" className="shrink-0">
            <circle
              cx="16"
              cy="16"
              r={radius}
              fill="none"
              stroke="var(--color-border-default)"
              strokeWidth={strokeWidth}
            />
            <circle
              cx="16"
              cy="16"
              r={radius}
              fill="none"
              stroke="var(--color-action-primary)"
              strokeWidth={strokeWidth}
              strokeLinecap="round"
              strokeDasharray={circumference}
              strokeDashoffset={strokeDashoffset}
              transform="rotate(-90 16 16)"
            />
          </svg>
          <span
            className="absolute inset-0 flex items-center justify-center text-[10px] font-medium text-text"
            data-testid="team-area-member-item-progress-count"
          >
            {taskProgress.completed}/{taskProgress.total}
          </span>
        </div>
      ) : isRunning ? (
        <svg className="w-4 h-4 text-info animate-spin shrink-0" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 2v4" />
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m16.2 7.8 2.9-2.9" />
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M18 12h4" />
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m16.2 16.2 2.9 2.9" />
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 18v4" />
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m4.9 19.1 2.9-2.9" />
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M2 12h4" />
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="m4.9 4.9 2.9 2.9" />
        </svg>
      ) : (
        <PendingIcon className="w-4 h-4 shrink-0 text-text-muted" />
      )}
    </button>
  );
}
