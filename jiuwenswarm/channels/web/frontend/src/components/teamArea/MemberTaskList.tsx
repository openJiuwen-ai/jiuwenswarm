import React, { useRef } from 'react';
import { useTranslation } from 'react-i18next';
import { StatusIcon, type MemberTask, type TaskStatus } from './shared';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import taskExpandIcon from '../../assets/work-mode/task-expand.svg';
import statusSuccessIcon from '../../assets/work-mode/status-success.svg';

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
          <img
            src={taskExpandIcon}
            alt=""
            aria-hidden="true"
            className={`h-4 w-4 shrink-0 ${expanded ? 'rotate-180' : ''}`}
          />
        </span>
      </div>
    </button>
  );
}

export function MemberTaskListPanel({
  tasks,
  emptyLabel = 'team.noMemberTasks',
}: {
  tasks: MemberTaskListItem[];
  emptyLabel?: string;
}) {
  const { t } = useTranslation();
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
      className="absolute bottom-full left-0 right-0 z-10 max-h-[258px] overflow-y-auto rounded-md border border-border bg-card p-4"
      style={{ boxShadow: '0 4px 10px 0 rgba(0, 0, 0, 0.12)' }}
      data-testid="team-area-member-detail-task-list-panel"
    >
      {tasks.length === 0 ? (
        <div className="py-2 text-center text-sm text-text-muted" data-testid="team-area-member-detail-task-list-empty">
          {t(emptyLabel)}
        </div>
      ) : (
        <ul className="space-y-1" data-testid="team-area-member-detail-task-list">
          {tasks.map((task) => (
            <li
              key={task.id}
              className="flex min-w-0 items-center gap-2"
              data-testid="team-area-member-detail-task-list-item"
              data-variant={task.id}
            >
              {task.status === 'completed' ? (
                <img src={statusSuccessIcon} className="h-4 w-4 shrink-0" aria-hidden="true" />
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
      )}
      {taskTitleTooltip}
    </div>
  );
}
