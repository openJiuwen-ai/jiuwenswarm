import React, { useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { StatusIcon, type MemberTask, type TaskStatus } from './shared';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import TaskExpandIcon from '../../assets/work-mode/task-expand.svg?react';

export type MemberTaskListItem = Pick<MemberTask, 'id' | 'title' | 'detail' | 'status' | 'raw' | 'updatedAt'> & {
  statusHistory?: Array<{ status: string; atMs?: number; source?: string }>;
};

export function MemberTaskListBar({
  tasks,
  expanded,
  onToggle,
}: {
  tasks: MemberTaskListItem[];
  expanded: boolean;
  onToggle: () => void;
}) {
  const { t } = useTranslation();
  const completedCount = tasks.filter((task) => task.status === 'completed').length;
  const latestTask = tasks.slice().sort((a, b) => {
    const aTime = typeof a.updatedAt === 'number' ? a.updatedAt : a.updatedAt ? Date.parse(a.updatedAt) : 0;
    const bTime = typeof b.updatedAt === 'number' ? b.updatedAt : b.updatedAt ? Date.parse(b.updatedAt) : 0;
    return bTime - aTime;
  })[0];

  return (
    <button
      type="button"
      onClick={onToggle}
      className="flex h-[54px] w-full items-center justify-between px-5 text-left hover:bg-secondary"
      aria-expanded={expanded}
      data-testid="team-area-member-task-bar-toggle"
    >
      <div className="flex min-w-0 items-center gap-6">
        <span className="text-sm font-semibold text-text">{t('team.memberTasks')}</span>
        {latestTask && (
          <div className="flex min-w-0 items-center gap-2">
            <StatusIcon status={latestTask.status as TaskStatus} />
            <span className="truncate text-sm text-muted-strong">{latestTask.title}</span>
          </div>
        )}
      </div>
      <div className="ml-4 flex shrink-0 items-center gap-4">
        <span className="text-sm text-muted">
          {completedCount}/{tasks.length}
        </span>
        <span className="text-muted">
          <TaskExpandIcon aria-hidden="true" className={`h-4 w-4 shrink-0 ${expanded ? 'rotate-180' : ''}`} />
        </span>
      </div>
    </button>
  );
}

export function MemberTaskListPanel({
  tasks,
}: {
  tasks: MemberTaskListItem[];
}) {
  const rowAnchorRef = useRef<HTMLLIElement | null>(null);
  const { tooltip: taskTitleTooltip, handlers: taskTitleTooltipHandlers } = useAdaptiveTooltip({ anchorRef: rowAnchorRef, align: 'right', offsetY: 2 });
  const taskTitleHandlers = {
    onMouseEnter: (event: React.MouseEvent<HTMLSpanElement>) => {
      const el = event.currentTarget;
      el.setAttribute('data-tooltip', el.scrollWidth > el.clientWidth ? el.textContent || '' : '');
      rowAnchorRef.current = el.parentElement as HTMLLIElement | null;
      taskTitleTooltipHandlers.onMouseEnter(event);
    },
    onMouseLeave: (event: React.MouseEvent<HTMLSpanElement>) => {
      event.currentTarget.setAttribute('data-tooltip', '');
      taskTitleTooltipHandlers.onMouseLeave();
    },
    onFocus: (event: React.FocusEvent<HTMLSpanElement>) => {
      const el = event.currentTarget;
      el.setAttribute('data-tooltip', el.scrollWidth > el.clientWidth ? el.textContent || '' : '');
      rowAnchorRef.current = el.parentElement as HTMLLIElement | null;
      taskTitleTooltipHandlers.onFocus(event);
    },
    onBlur: (event: React.FocusEvent<HTMLSpanElement>) => {
      event.currentTarget.setAttribute('data-tooltip', '');
      taskTitleTooltipHandlers.onBlur();
    },
  };

  return (
    <div
      className="absolute bottom-full left-0 right-0 z-10 max-h-[258px] overflow-y-auto rounded-md border border-border bg-card p-4 shadow-[var(--effect-shadow-md)]"
      data-testid="team-area-member-detail-task-list-panel"
    >
      <ul className="space-y-1" data-testid="team-area-member-detail-task-list">
        {tasks.map((task) => (
          <li
            key={task.id}
            className="flex min-w-0 items-center gap-2"
            data-testid="team-area-member-detail-task-list-item"
            data-variant={task.id}
          >
            {task.status === 'completed' ? (
              <svg
                viewBox="0 0 16 16"
                fill="none"
                className="h-4 w-4 shrink-0 text-[var(--color-team-status-completed)]"
                aria-hidden="true"
              >
                <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="0.981849" />
                <path
                  d="M4.86328 7.72751L7.20336 10.1821L11.3517 5.81836"
                  stroke="currentColor"
                  strokeWidth="0.981849"
                  strokeLinecap="round"
                  strokeLinejoin="round"
                />
              </svg>
            ) : (
              <StatusIcon status={task.status as TaskStatus} />
            )}
            <span
              className="min-w-0 flex-1 truncate text-sm text-text-meta leading-[22px]"
              data-testid="team-area-member-detail-task-list-item-title"
              {...taskTitleHandlers}
            >
              {task.title}
            </span>
          </li>
        ))}
      </ul>
      {taskTitleTooltip}
    </div>
  );
}
