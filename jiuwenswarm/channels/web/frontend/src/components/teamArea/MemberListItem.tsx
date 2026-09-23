import { TeamMemberAvatar } from '../TeamMemberAvatar';
import PendingIcon from '../../assets/pending.svg?react';
import LoadingIcon from '../../assets/subagent/loading.svg?react';
import { getMemberPlainName, getMemberStatusKey, type TeamMember } from './shared';

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

  const progressPercent =
    taskProgress && taskProgress.total > 0 ? Math.round((taskProgress.completed / taskProgress.total) * 100) : 0;
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
      className={`flex w-full items-center gap-3 rounded-md text-left  ${compact ? 'p-2' : 'px-[8px] py-[9px]'} ${
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
        <div
          className="mt-0.5 truncate text-xs text-[var(--color-team-member-item-id-text)]"
          data-testid="team-area-member-item-id"
        >
          @{member.member_id}
        </div>
      </div>
      {compact ? (
        isRunning ? (
          <LoadingIcon className="h-4 w-4 shrink-0 text-muted animate-spin" />
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
        <LoadingIcon className="h-4 w-4 shrink-0 text-muted animate-spin" />
      ) : (
        <PendingIcon className="w-4 h-4 shrink-0 text-text-muted" />
      )}
    </button>
  );
}
