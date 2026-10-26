import { useTranslation } from 'react-i18next';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import pendingIcon from '../../assets/pending.svg';
import {
  getMemberDisplayName,
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
  const { t } = useTranslation();
  const displayName = getMemberDisplayName(member);
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
      className={`flex w-full items-center gap-3 rounded-md text-left  ${
        compact ? 'p-2' : 'p-3'
      } ${
        selected
          ? 'border border-accent bg-accent-subtle'
          : 'border border-transparent hover:bg-secondary'
      }`}
    >
      <div className="relative shrink-0">
        <TeamMemberAvatar
          member={member.member_id}
          alt={displayName}
          className={`${compact ? 'h-8 w-8' : 'h-10 w-10'} rounded-full`}
          imageClassName="rounded-full"
        />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-2">
          <span className={`${compact ? 'text-xs' : 'text-sm'} truncate font-medium text-text`}>
            {displayName}
          </span>
        </div>
        {!compact && member.mode && (
          <div className="mt-0.5 truncate text-xs text-text-muted">
            {t('team.runningMode', { mode: member.mode })}
          </div>
        )}
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
          <img src={pendingIcon} alt="" className="w-4 h-4" />
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
          <span className="absolute inset-0 flex items-center justify-center text-[10px] font-medium text-text">
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
        <img src={pendingIcon} alt="" className="w-4 h-4 shrink-0" />
      )}
    </button>
  );
}
