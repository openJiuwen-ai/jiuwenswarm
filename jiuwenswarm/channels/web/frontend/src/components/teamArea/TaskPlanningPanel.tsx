import { useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { File, Maximize2, Puzzle } from 'lucide-react';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import type { TeamTask as SessionTeamTask } from '../../stores/sessionStore';
import recentTasksIcon from '../../assets/work-mode/recent-tasks.svg';
import statusProcessingIcon from '../../assets/work-mode/status-processing.svg';
import statusSuccessIcon from '../../assets/work-mode/status-success.svg';
import statusWaitingIcon from '../../assets/work-mode/status-waiting.svg';
import statusWarningIcon from '../../assets/work-mode/status-warning.svg';
import ListViewIcon from '../../assets/work-mode/view-list.svg?react';
import BoardViewIcon from '../../assets/work-mode/view-board.svg?react';
import {
  BOARD_COLUMNS,
  getBoardTaskContent,
  getBoardTaskTitle,
  getMemberDisplayName,
  getTaskColumnKey,
  type TaskColumnKey,
  type TeamMember,
} from './shared';
import { getTotalTaskVisualProgressPercent } from './taskProgress';

type TaskPlanningPanelProps = {
  variant: 'compact' | 'expanded';
  tasks: SessionTeamTask[];
  progressTasks?: SessionTeamTask[];
  members: TeamMember[];
  totalTasks: number;
  completedTasks: number;
  onExpand?: () => void;
  /** 紧凑态下隐藏右上角展开按钮（用于非集群模式复用本面板时） */
  hideExpandButton?: boolean;
  /** 紧凑态下隐藏任务行负责人头像（用于非集群模式复用本面板时） */
  hideAssignee?: boolean;
  /** 紧凑态下隐藏底部边框（用于非集群模式复用本面板时） */
  hideBorder?: boolean;
  /** 自定义标题（不传则默认用 team.taskOverview） */
  title?: string;
};

const compactStatusIcons: Record<TaskColumnKey, string> = {
  completed: statusSuccessIcon,
  running: statusProcessingIcon,
  waiting: statusWaitingIcon,
  cancelled: statusWarningIcon,
};

export function TaskPlanningPanel({
  variant,
  tasks,
  progressTasks = tasks,
  members,
  totalTasks,
  completedTasks,
  onExpand,
  hideExpandButton = false,
  hideAssignee = false,
  hideBorder = false,
  title,
}: TaskPlanningPanelProps) {
  const { t } = useTranslation();
  const [now, setNow] = useState(() => Date.now());
  const [view, setView] = useState<'board' | 'list'>('board');
  const groupedTasks = useMemo(() => {
    const groups: Record<TaskColumnKey, SessionTeamTask[]> = {
      waiting: [],
      running: [],
      completed: [],
      cancelled: [],
    };

    tasks.forEach((task) => {
      groups[getTaskColumnKey(task)].push(task);
    });

    return groups;
  }, [tasks]);

  // 按后端 todos 数组顺序的全局序号：展开态与收起态共用同一份，保证同一任务在两种状态下序号一致。
  // 后端在状态变更时保序，仅在显式新增/插入任务时改变顺序，序号稳定。
  const globalIndexMap = useMemo(() => {
    const map = new Map<string, number>();
    tasks.forEach((task, index) => {
      map.set(task.task_id, index + 1);
    });
    return map;
  }, [tasks]);

  useEffect(() => {
    if (variant !== 'expanded') {
      return undefined;
    }
    const timer = window.setInterval(() => setNow(Date.now()), 3_000);
    return () => window.clearInterval(timer);
  }, [variant]);

  const completedProgressTasks = progressTasks.filter((task) => task.status === 'completed').length;
  const completedProgressPercent = progressTasks.length > 0
    ? Math.round((completedProgressTasks / progressTasks.length) * 100)
    : 0;
  const progressPercent = variant === 'expanded'
    ? getTotalTaskVisualProgressPercent(progressTasks, now)
    : completedProgressPercent;

  if (variant === 'compact') {
    const allTasks = tasks;

    return (
      <div
        className={`flex flex-[2] flex-col overflow-hidden min-h-0 px-3 pb-3${hideBorder ? '' : ' border-b border-border'}`}
        data-testid="team-area-task-planning-panel"
        data-variant="compact"
      >
        <div className="flex w-full shrink-0 items-center justify-between bg-card px-4 py-3" data-testid="team-area-task-planning-header">
          <div className="flex items-center gap-2">
            <img src={recentTasksIcon} width={16} height={16} aria-hidden="true" />
            <span className="text-sm font-medium text-text" data-testid="team-area-task-planning-title">{title ?? t('team.taskOverview')}</span>
          </div>
          {hideExpandButton ? null : (
            <button
              onClick={onExpand}
              data-testid="team-area-task-planning-expand-button"
              className="rounded p-2 text-text-muted  hover:bg-secondary hover:text-text"
              title={t('team.expand')}
            >
              <Maximize2 size={12} aria-hidden="true" />
            </button>
          )}
        </div>
        <div className="px-4 py-3 shrink-0" data-testid="team-area-task-planning-progress">
          {allTasks.length > 0 && (
            <div className="mb-4">
              <div className="flex items-center justify-start mb-2">
                <div className="flex items-baseline gap-1">
                  <span className="text-lg font-semibold text-text-strong" data-testid="team-area-task-planning-completed-count">{completedTasks}</span>
                  <span className="text-sm text-text-muted" data-testid="team-area-task-planning-total-count">/ {totalTasks}</span>
                </div>
              </div>
              <div className="h-2 bg-secondary rounded-full overflow-hidden" data-testid="team-area-task-planning-progress-track">
                <div
                  className="h-full bg-accent rounded-full  "
                  style={{ width: `${progressPercent}%` }}
                  data-testid="team-area-task-planning-progress-fill"
                />
              </div>
            </div>
          )}
          <div className="flex justify-between gap-2">
            {BOARD_COLUMNS.map((column) => (
              <div
                key={column.key}
                data-testid="team-area-task-planning-column-stat"
                data-variant={column.key}
                className={`flex-1 flex flex-col items-center justify-center py-2 rounded-md`}
              >
                <span className="text-sm font-normal text-text-strong" data-testid="team-area-task-planning-column-count">{groupedTasks[column.key].length}</span>
                <span className="text-xs mt-1 text-text-muted" data-testid="team-area-task-planning-column-label">{t(column.labelKey)}</span>
              </div>
            ))}
          </div>
        </div>
        <div className="flex-1 overflow-y-auto px-4 pb-3">
          {allTasks.length === 0 ? (
            <div className="text-center py-8 text-sm text-text-muted" data-testid="team-area-task-planning-empty">
              {t('team.noTasks')}
            </div>
          ) : (
            <div className="space-y-2">
              {allTasks.map((task) => {
                const assigneeExists = Boolean(task.assignee && members.some(member => member.member_id === task.assignee));
                const assigneeName = getMemberDisplayName(task.assignee || '');
                const title = getBoardTaskTitle(task);
                const columnKey = getTaskColumnKey(task);
                const seq = globalIndexMap.get(task.task_id) ?? 0;
                return (
                  <div
                    key={task.task_id}
                    data-testid="team-area-task-planning-task-row"
                    data-variant={task.task_id}
                    className="flex items-center gap-3 px-3 py-2 rounded-md"
                  >
                    <span
                      className="inline-flex items-center justify-center w-[20px] h-[20px] text-xs font-medium text-muted rounded-[16px] bg-[var(--color-task-index-surface)]"
                      data-testid="team-area-task-planning-task-seq"
                    >
                      {String(seq).padStart(2, '0')}
                    </span>
                    {!hideAssignee && (
                      assigneeExists ? (
                        <TeamMemberAvatar
                          member={task.assignee}
                          alt={assigneeName}
                          className="h-4 w-4 rounded-full shrink-0"
                          imageClassName="rounded-full"
                        />
                      ) : (
                        <UnassignedTeamAvatar className="h-4 w-4 rounded-full shrink-0" />
                      )
                    )}
                    <span className="flex-1 text-xs text-text truncate" data-testid="team-area-task-planning-task-title">{title}</span>
                    <img
                      src={compactStatusIcons[columnKey]}
                      className={`h-4 w-4 shrink-0 ${columnKey === 'running' ? 'animate-spin' : ''}`}
                      aria-hidden="true"
                      data-testid="team-area-task-planning-task-status-icon"
                    />
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    );
  }

  const viewSwitcher = (
    <div className="flex items-center gap-1" role="group" aria-label={t('team.planning.progressTitle')} data-testid="team-area-task-planning-view-switcher">
      <button
        type="button"
        onClick={() => setView('list')}
        data-testid="team-area-task-planning-view-list-button"
        className={`flex h-8 w-8 items-center justify-center rounded-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-card ${view === 'list' ? 'bg-secondary text-text' : 'text-text-muted hover:bg-secondary/50 hover:text-text'}`}
        aria-label={t('team.planning.views.list')}
        title={t('team.planning.views.list')}
        aria-pressed={view === 'list'}
      >
        <ListViewIcon className="h-4 w-4 shrink-0" aria-hidden="true" />
      </button>
      <button
        type="button"
        onClick={() => setView('board')}
        data-testid="team-area-task-planning-view-board-button"
        className={`flex h-8 w-8 items-center justify-center rounded-sm transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-card ${view === 'board' ? 'bg-secondary text-text' : 'text-text-muted hover:bg-secondary/50 hover:text-text'}`}
        aria-label={t('team.planning.views.board')}
        title={t('team.planning.views.board')}
        aria-pressed={view === 'board'}
      >
        <BoardViewIcon className="h-4 w-4 shrink-0" aria-hidden="true" />
      </button>
    </div>
  );

  return (
    <div className="flex-1 overflow-hidden bg-card" data-testid="team-area-task-planning-panel" data-variant="expanded">
      {view === 'list' ? (
        <div className="flex h-full flex-col px-6 pb-6" data-testid="team-area-task-planning-list-view">
          <div className="flex h-8 items-center gap-3">
            <h2 className="text-sm font-medium leading-5 text-text-strong" data-testid="team-area-task-planning-view-title">{t('team.planning.progressTitle')}</h2>
            {viewSwitcher}
          </div>
          <ExpandedTaskList
            tasks={tasks}
            groupedTasks={groupedTasks}
            globalIndexMap={globalIndexMap}
            progressPercent={progressPercent}
          />
        </div>
      ) : (
        <div className="flex h-full flex-col px-6 pb-6">
          <div className="mb-5 flex h-8 items-center gap-3">
            <h2 className="text-sm font-medium leading-5 text-text-strong" data-testid="team-area-task-planning-view-title">{t('team.planning.progressTitle')}</h2>
            {viewSwitcher}
            <span className="text-sm font-medium text-text-strong" data-testid="team-area-task-planning-progress-percent">{progressPercent}%</span>
          </div>

          <div className="min-h-0 flex-1 overflow-y-auto rounded-lg bg-secondary p-6" data-testid="team-area-task-planning-board">
            <div
              className="grid min-w-[920px] gap-5"
              style={{ gridTemplateColumns: 'repeat(4, minmax(220px, 1fr))' }}
              data-testid="team-area-task-planning-board-columns"
            >
              {BOARD_COLUMNS.map((column) => (
                <BoardColumn
                  key={column.key}
                  column={column}
                  tasks={groupedTasks[column.key]}
                  members={members}
                  hideAssignee={hideAssignee}
                  globalIndexMap={globalIndexMap}
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function ExpandedTaskList({
  tasks,
  groupedTasks,
  globalIndexMap,
  progressPercent,
}: {
  tasks: SessionTeamTask[];
  groupedTasks: Record<TaskColumnKey, SessionTeamTask[]>;
  globalIndexMap: Map<string, number>;
  progressPercent: number;
}) {
  const { t } = useTranslation();

  return (
    <>
      <div className="mt-2 flex min-h-7 flex-wrap items-baseline gap-x-8 gap-y-2" data-testid="team-area-task-planning-list-metrics">
        <div className="flex shrink-0 items-baseline gap-2">
          <span className="text-sm leading-5 text-text-muted" data-testid="team-area-task-planning-list-metrics-progress-label">{t('team.planning.metrics.progress')}</span>
          <span className="text-base font-semibold leading-6 text-text" data-testid="team-area-task-planning-list-metrics-progress-value">{progressPercent}%</span>
        </div>
        {BOARD_COLUMNS.map((column) => (
          <div key={column.key} className="flex shrink-0 items-baseline gap-2" data-testid="team-area-task-planning-list-metrics-column" data-variant={column.key}>
            <span className="text-sm leading-5 text-text-muted" data-testid="team-area-task-planning-list-metrics-column-label">{t(column.labelKey)}</span>
            <span className="text-base font-semibold leading-6 text-text" data-testid="team-area-task-planning-list-metrics-column-count">{groupedTasks[column.key].length}</span>
          </div>
        ))}
      </div>
      <div className="h-1 overflow-hidden rounded-full bg-secondary" data-testid="team-area-task-planning-list-progress-track">
        <div
          className={`h-full rounded-full transition-all duration-300 ${tasks.length > 0 && groupedTasks.completed.length === tasks.length ? 'bg-ok' : 'bg-accent'}`}
          style={{ width: `${progressPercent}%` }}
          data-testid="team-area-task-planning-list-progress-fill"
        />
      </div>
      <div className="mt-3 min-h-0 flex-1 overflow-x-hidden overflow-y-auto" data-testid="team-area-task-planning-list-task-list">
        {tasks.length === 0 ? (
          <div className="py-8 text-center text-sm text-text-muted" data-testid="team-area-task-planning-list-empty">{t('team.noTasks')}</div>
        ) : tasks.map((task) => {
          const columnKey = getTaskColumnKey(task);
          const seq = globalIndexMap.get(task.task_id) ?? 0;
          const title = getBoardTaskTitle(task);

          return (
            <div
              key={task.task_id}
              className="flex h-12 items-center"
              data-testid="team-area-task-planning-list-task-row"
              data-variant={task.task_id}
            >
              <span className="mr-4 inline-flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-secondary text-xs font-medium leading-4 text-muted">
                {String(seq).padStart(2, '0')}
              </span>
              <span className="min-w-0 flex-1 truncate text-sm font-normal leading-5 text-text" title={title}>{title}</span>
              <img
                src={compactStatusIcons[columnKey]}
                className={`ml-4 h-4 w-4 shrink-0 ${columnKey === 'running' ? 'animate-spin' : ''}`}
                aria-hidden="true"
              />
            </div>
          );
        })}
      </div>
    </>
  );
}

function BoardColumn({
  column,
  tasks,
  members,
  hideAssignee,
  globalIndexMap,
}: {
  column: typeof BOARD_COLUMNS[number];
  tasks: SessionTeamTask[];
  members: TeamMember[];
  hideAssignee: boolean;
  globalIndexMap: Map<string, number>;
}) {
  const { t } = useTranslation();

  return (
    <section className="min-w-0" data-testid="team-area-task-planning-board-column" data-variant={column.key}>
      <div
        className={`mb-3 inline-flex h-7 items-center rounded-full px-4 text-sm font-medium shadow-[var(--effect-task-column-pill-shadow)] ${column.pillClassName}`}
        data-testid="team-area-task-planning-board-column-title"
      >
        <span className={`mr-2 h-1.5 w-1.5 rounded-full ${column.dotClassName}`} />
        {t(column.labelKey)} {tasks.length}
      </div>
      <div className="space-y-3" data-testid="team-area-task-planning-board-column-task-list">
        {tasks.map((task) => {
          const seq = globalIndexMap.get(task.task_id) ?? 0;
          return (
            <BoardTaskCard
              key={task.task_id}
              task={task}
              members={members}
              hideAssignee={hideAssignee}
              index={seq}
            />
          );
        })}
      </div>
    </section>
  );
}

function BoardTaskCard({
  task,
  members,
  hideAssignee,
  index,
}: {
  task: SessionTeamTask;
  members: TeamMember[];
  hideAssignee: boolean;
  index: number;
}) {
  const assigneeExists = Boolean(task.assignee && members.some(member => member.member_id === task.assignee));
  const assigneeName = getMemberDisplayName(task.assignee || '');
  const title = getBoardTaskTitle(task);
  const content = getBoardTaskContent(task);

  return (
    <article
      className="rounded-2xl border border-border bg-[var(--color-task-card-surface)] p-1 shadow-sm"
      data-testid="team-area-task-planning-board-task-card"
      data-variant={task.task_id}
    >
      <div className="rounded-2xl border border-border bg-card px-4 py-4">
        <h3 className="truncate text-base font-medium leading-[18px] text-text-strong" title={title} data-testid="team-area-task-planning-board-task-title">
          {title}
        </h3>
        {content ? (
          <p className="mt-2 line-clamp-2 text-sm leading-5 text-text-muted" title={content} data-testid="team-area-task-planning-board-task-content">
            {content}
          </p>
        ) : null}
        <TaskResourcePanel skills={task.skills} files={task.files} />
      </div>
      <div className="mt-3 flex h-8 items-center bg-[var(--color-task-card-surface)] px-1 pb-1" data-testid="team-area-task-planning-board-task-footer">
        {hideAssignee ? (
          <span
            className="inline-flex h-[20px] w-[20px] items-center justify-center rounded-[16px] bg-[var(--color-task-index-surface)] text-xs font-medium text-muted"
            data-testid="team-area-task-planning-board-task-footer-seq"
            data-variant="seq"
          >
            {String(index).padStart(2, '0')}
          </span>
        ) : assigneeExists ? (
          <div title={assigneeName}>
            <TeamMemberAvatar
              member={task.assignee}
              alt={assigneeName}
              className="h-8 w-8 rounded-full"
              imageClassName="rounded-full"
            />
          </div>
        ) : (
          <UnassignedTeamAvatar className="h-8 w-8 rounded-full" />
        )}
      </div>
    </article>
  );
}

function UnassignedTeamAvatar({
  className,
}: {
  className?: string;
}) {
  const { t } = useTranslation();

  return (
    <div
      className={`flex shrink-0 items-center justify-center overflow-hidden border border-border bg-card text-[12px] font-medium text-muted ${className || ''}`}
      aria-label={t('team.planning.unassignedAvatar')}
      title={t('team.planning.unassigned')}
    >
      --
    </div>
  );
}

function TaskResourcePanel({
  skills,
  files,
}: {
  skills?: string[];
  files?: string[];
}) {
  const { t } = useTranslation();
  const skillCount = skills?.length ?? 0;
  const fileCount = files?.length ?? 0;
  const hasSkills = skillCount > 0;
  const hasFiles = fileCount > 0;
  const [activeTab, setActiveTab] = useState<'skills' | 'files'>('skills');

  if (!hasSkills && !hasFiles) {
    return null;
  }

  let resolvedActiveTab: 'skills' | 'files' = 'files';
  if (activeTab === 'files' && hasFiles) {
    resolvedActiveTab = 'files';
  } else if (hasSkills) {
    resolvedActiveTab = 'skills';
  }
  const activeItems = resolvedActiveTab === 'skills' ? skills : files;

  return (
    <div className="mt-4 rounded-lg bg-secondary px-3 py-3" data-testid="team-area-task-planning-board-task-resources">
      <div className="flex h-6 items-center gap-4 border-b border-border" role="tablist" aria-label={t('team.planning.resources')} data-testid="team-area-task-planning-resources-tabs">
        {hasSkills && (
          <ResourceTab
            label={t('team.planning.skills')}
            count={skillCount}
            active={resolvedActiveTab === 'skills'}
            onClick={() => setActiveTab('skills')}
          />
        )}
        {hasFiles && (
          <ResourceTab
            label={t('team.planning.files')}
            count={fileCount}
            active={resolvedActiveTab === 'files'}
            onClick={() => setActiveTab('files')}
          />
        )}
      </div>
      <div className="min-h-[44px] pt-3">
        {activeItems?.map((item) => (
          <ResourceLine
            key={`${resolvedActiveTab}-${item}`}
            icon={resolvedActiveTab === 'skills' ? <Puzzle className="h-4 w-4 shrink-0 text-muted" aria-hidden="true" /> : <File className="h-4 w-4 shrink-0 text-muted" aria-hidden="true" />}
            label={item}
          />
        ))}
      </div>
    </div>
  );
}

function ResourceTab({
  label,
  count,
  active = false,
  onClick,
}: {
  label: string;
  count: number;
  active?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      className="relative flex h-6 items-start gap-1 text-xs focus:outline-none focus-visible:ring-2 focus-visible:ring-accent/40"
      onClick={onClick}
      role="tab"
      aria-selected={active}
    >
      <span className={active ? 'font-medium text-text-strong' : 'text-text'}>
        {label}
      </span>
      <span className="flex h-4 min-w-4 items-center justify-center rounded-full bg-secondary px-1 text-[10px] leading-4 text-text-strong">
        {count}
      </span>
      {active && <span className="absolute -bottom-px left-0 h-0.5 w-6 bg-text-strong" />}
    </button>
  );
}

function ResourceLine({
  icon,
  label,
}: {
  icon: ReactNode;
  label: string;
}) {
  return (
    <div
      className="mb-2 flex items-center gap-1 text-xs text-text last:mb-0"
    >
      {icon}
      <span className="truncate">{label}</span>
    </div>
  );
}
