import { useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { File, GitBranch, Maximize2, Puzzle } from 'lucide-react';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { useChatStore, useSessionStore } from '../../stores';
import type { TeamTask as SessionTeamTask } from '../../stores/sessionStore';
import recentTasksIcon from '../../assets/work-mode/recent-tasks.svg';
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
import { CompactTaskList } from './CompactTaskList';
import { UnassignedTeamAvatar } from './UnassignedTeamAvatar';
import { getTotalTaskVisualProgressPercent } from './taskProgress';
import { useAdaptiveTooltip } from '../../hooks/useAdaptiveTooltip';
import { SwarmflowTreeView } from './SwarmflowTreeView';
import { SwarmflowGraphView } from './SwarmflowGraphView';
import type { WorkflowRun } from './workflowTypes';

type TaskPlanningPanelProps = {
  variant: 'compact' | 'expanded';
  tasks: SessionTeamTask[];
  progressTasks?: SessionTeamTask[];
  now?: number;
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
  /** 紧凑态下隐藏头部（用于外层 CollapsibleSection 提供头部时） */
  hideHeader?: boolean;
  /** 紧凑态下把任务行状态图标放到行尾（默认在行首） */
  statusIconAtEnd?: boolean;
  /** 自定义标题（不传则默认用 team.taskOverview） */
  title?: string;
  /** 耗时文本（紧凑态进度区右侧显示） */
  duration?: string;
  /** 自定义任务行中状态图标与标题之间的内容（如人物图标） */
  renderTaskIcon?: (task: SessionTeamTask) => ReactNode;
  /** 折叠态最大显示任务数（透传给 CompactTaskList） */
  maxCollapsedCount?: number;
  /** 是否已展开全部（透传给 CompactTaskList） */
  expanded?: boolean;
  /** 空列表时显示的插图 URL（透传给 CompactTaskList） */
  emptyIllustration?: string;
};

const COLUMN_STATS: Array<{ key: TaskColumnKey | 'progress'; labelKey: string }> = [
  { key: 'progress', labelKey: 'team.planning.metrics.progress' },
  { key: 'completed', labelKey: 'team.planning.columns.completed' },
  { key: 'running', labelKey: 'team.planning.columns.running' },
  { key: 'waiting', labelKey: 'team.planning.columns.waiting' },
  { key: 'cancelled', labelKey: 'team.planning.columns.failed' },
];

export function ProgressBar({
  progressPercent,
  groupedTasks,
}: {
  progressPercent: number;
  groupedTasks: Record<TaskColumnKey, SessionTeamTask[]>;
}) {
  const { t } = useTranslation();
  return (
    <>
      <div
        className="flex flex-wrap items-baseline gap-x-8 gap-y-2 mb-2"
        data-testid="team-area-task-planning-progress"
      >
        <div className="flex justify-between gap-[22px]">
          {COLUMN_STATS.map((column) => (
            <div
              key={column.key}
              data-testid="team-area-task-planning-column-stat"
              data-variant={column.key}
              className="flex items-center gap-2.5"
            >
              <span
                className="text-xs"
                style={{ color: 'var(--color-task-column-label)' }}
                data-testid="team-area-task-planning-column-label"
              >
                {t(column.labelKey)}
              </span>
              <span
                className="text-sm font-semibold text-text-strong"
                data-testid="team-area-task-planning-column-count"
              >
                {column.key === 'progress' ? `${progressPercent}%` : groupedTasks[column.key].length}
              </span>
            </div>
          ))}
        </div>
      </div>
      <div
        className="h-1 rounded-full overflow-hidden mb-4"
        style={{ backgroundColor: 'var(--color-task-progress-track)' }}
        data-testid="team-area-task-planning-progress-track"
      >
        <div
          className="h-full rounded-full transition-all duration-300"
          style={{ width: `${progressPercent}%`, backgroundColor: 'var(--color-task-progress)' }}
          data-testid="team-area-task-planning-progress-fill"
        />
      </div>
    </>
  );
}

export function ProgressSection({
  tasks,
  progressTasks,
  now,
  groupedTasks,
  completedTasks,
  totalTasks,
  displayMode = 'percent',
  members,
  hideAssignee,
  statusIconAtEnd,
  renderTaskIcon,
  maxCollapsedCount,
  expanded,
  emptyIllustration,
  workflowRuns,
}: {
  tasks: SessionTeamTask[];
  progressTasks?: SessionTeamTask[];
  now?: number;
  groupedTasks: Record<TaskColumnKey, SessionTeamTask[]>;
  completedTasks: number;
  totalTasks: number;
  displayMode?: 'count' | 'percent';
  members: TeamMember[];
  hideAssignee?: boolean;
  statusIconAtEnd?: boolean;
  renderTaskIcon?: (task: SessionTeamTask) => ReactNode;
  maxCollapsedCount?: number;
  expanded?: boolean;
  emptyIllustration?: string;
  workflowRuns?: WorkflowRun[];
}) {
  const { t } = useTranslation();
  const emptyIllustrationSize = displayMode === 'count' ? 48 : 72;

  if (tasks.length === 0) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 text-center text-sm text-text-muted"
        style={{ height: 120 }}
        data-testid="team-area-task-planning-empty"
      >
        {emptyIllustration && (
          <img
            src={emptyIllustration}
            alt=""
            width={emptyIllustrationSize}
            height={emptyIllustrationSize}
            className="shrink-0"
          />
        )}
        <span>{t('team.noTasks')}</span>
      </div>
    );
  }

  const progressPercent = getTotalTaskVisualProgressPercent(progressTasks ?? tasks, now ?? Date.now());

  if (displayMode === 'count') {
    const hasWorkflow = Boolean(workflowRuns && workflowRuns.length > 0);
    return (
      <div className="flex flex-col flex-1 min-h-0" data-testid="team-area-task-planning-progress-section">
        <div className="shrink-0" data-testid="team-area-task-planning-progress">
          <div className="mb-4">
            <div className="flex items-center justify-between mb-2">
              <div className="flex items-end gap-1 text-text-strong">
                <span
                  className="text-2xl font-semibold leading-none"
                  data-testid="team-area-task-planning-completed-count"
                >
                  {completedTasks}
                </span>
                <span className="text-sm leading-none pb-0.5" data-testid="team-area-task-planning-total-count">
                  / {totalTasks}
                </span>
              </div>
            </div>
            <div
              className="h-1 rounded-full overflow-hidden"
              style={{ backgroundColor: 'var(--color-task-progress-track)' }}
              data-testid="team-area-task-planning-progress-track"
            >
              <div
                className="h-full rounded-full"
                style={{ width: `${progressPercent}%`, backgroundColor: 'var(--color-task-progress)' }}
                data-testid="team-area-task-planning-progress-fill"
              />
            </div>
          </div>
        </div>
        <div className="flex-1 overflow-y-auto">
          {hasWorkflow && workflowRuns ? (
            workflowRuns.map((run) => {
              const runTasks = tasks.filter((t) => t.workflow_run_id === run.id);
              if (runTasks.length === 0) return null;
              const maxPerRun = 4;
              const visible = expanded ? runTasks : runTasks.slice(0, maxPerRun);
              const remaining = expanded ? 0 : runTasks.length - visible.length;
              return (
                <div key={run.id} className="border-b border-border last:border-b-0">
                  <div className="px-2 py-1.5 text-xs text-text-muted font-medium">{run.name ?? run.id}</div>
                  <CompactTaskList
                    tasks={visible}
                    members={members}
                    hideAssignee={hideAssignee ?? false}
                    statusIconAtEnd={statusIconAtEnd}
                    renderTaskIcon={renderTaskIcon}
                    maxCollapsedCount={maxCollapsedCount}
                    expanded={expanded}
                    emptyText={t('team.noTasks')}
                    emptyIllustration={emptyIllustration}
                  />
                  {remaining > 0 && (
                    <div className="px-2 py-1 text-xs text-text-muted">{t('team.moreTasks', { count: remaining })}</div>
                  )}
                </div>
              );
            })
          ) : (
            <CompactTaskList
              tasks={tasks}
              members={members}
              hideAssignee={hideAssignee ?? false}
              statusIconAtEnd={statusIconAtEnd}
              renderTaskIcon={renderTaskIcon}
              maxCollapsedCount={maxCollapsedCount}
              expanded={expanded}
              emptyText={t('team.noTasks')}
              emptyIllustration={emptyIllustration}
            />
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="flex flex-col flex-1 min-h-0" data-testid="team-area-task-planning-progress-section">
      <ProgressBar progressPercent={progressPercent} groupedTasks={groupedTasks} />
      <div className="flex-1 overflow-y-auto" data-testid="team-area-task-planning-task-list">
        <CompactTaskList
          tasks={tasks}
          members={members}
          hideAssignee={hideAssignee ?? false}
          statusIconAtEnd={statusIconAtEnd}
          renderTaskIcon={renderTaskIcon}
          maxCollapsedCount={maxCollapsedCount}
          expanded={expanded}
          emptyText={t('team.noTasks')}
          emptyIllustration={emptyIllustration}
        />
      </div>
    </div>
  );
}

export function ViewSwitcher({
  view,
  onViewChange,
  showWorkflow = false,
}: {
  view: 'board' | 'list' | 'workflow';
  onViewChange: (view: 'board' | 'list' | 'workflow') => void;
  showWorkflow?: boolean;
}) {
  const { t } = useTranslation();
  const { tooltip, handlers: tooltipHandlers } = useAdaptiveTooltip();
  return (
    <div
      className="flex items-center gap-1 rounded-[4px] bg-secondary p-1"
      role="group"
      aria-label={t('team.planning.progressTitle')}
      data-testid="team-area-task-planning-view-switcher"
    >
      <button
        type="button"
        onClick={() => onViewChange('list')}
        data-testid="team-area-task-planning-view-list-button"
        className={`flex h-6 w-6 items-center justify-center rounded-[4px] transition-colors ${view === 'list' ? 'bg-card text-text shadow-sm' : 'text-text-muted hover:text-text'}`}
        aria-label={t('team.planning.views.list')}
        data-tooltip={t('team.planning.views.list')}
        aria-pressed={view === 'list'}
        {...tooltipHandlers}
      >
        <ListViewIcon className="h-4 w-4 shrink-0" aria-hidden="true" />
      </button>
      <button
        type="button"
        onClick={() => onViewChange('board')}
        data-testid="team-area-task-planning-view-board-button"
        className={`flex h-6 w-6 items-center justify-center rounded-[4px] transition-colors ${view === 'board' ? 'bg-card text-text shadow-sm' : 'text-text-muted hover:text-text'}`}
        aria-label={t('team.planning.views.board')}
        data-tooltip={t('team.planning.views.board')}
        aria-pressed={view === 'board'}
        {...tooltipHandlers}
      >
        <BoardViewIcon className="h-4 w-4 shrink-0" aria-hidden="true" />
      </button>
      {tooltip}
      {showWorkflow && (
        <button
          type="button"
          onClick={() => onViewChange('workflow')}
          data-testid="team-area-task-planning-view-workflow-button"
          className={`flex h-6 w-6 items-center justify-center rounded-[4px] transition-colors ${view === 'workflow' ? 'bg-card text-text shadow-sm' : 'text-text-muted hover:text-text'}`}
          aria-label={t('team.planning.views.workflow')}
          title={t('team.planning.views.workflow')}
          aria-pressed={view === 'workflow'}
        >
          <GitBranch className="h-4 w-4 shrink-0" aria-hidden="true" />
        </button>
      )}
    </div>
  );
}

export function TaskPlanningPanel({
  variant,
  tasks,
  progressTasks,
  now,
  members,
  totalTasks,
  completedTasks,
  onExpand,
  hideExpandButton = false,
  hideAssignee = false,
  hideBorder = false,
  hideHeader = false,
  statusIconAtEnd = false,
  title,
  renderTaskIcon,
  maxCollapsedCount,
  expanded,
  emptyIllustration,
}: TaskPlanningPanelProps) {
  const { t } = useTranslation();
  const [view, setView] = useState<'board' | 'list' | 'workflow'>('list');
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const workflowRuns = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.workflowRuns ?? []);
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

  const progressPercent = getTotalTaskVisualProgressPercent(progressTasks ?? tasks, now ?? Date.now());

  if (variant === 'compact') {
    const allTasks = tasks;

    return (
      <div
        className={`flex flex-[2] flex-col overflow-hidden min-h-0 ${hideBorder ? '' : ' border-b border-border'}`}
        data-testid="team-area-task-planning-panel"
        data-variant="compact"
      >
        {hideHeader ? null : (
          <div
            className="flex w-full shrink-0 items-center justify-between bg-card px-4 py-3"
            data-testid="team-area-task-planning-header"
          >
            <div className="flex items-center gap-2">
              <img src={recentTasksIcon} width={16} height={16} aria-hidden="true" />
              <span className="text-sm font-medium text-text" data-testid="team-area-task-planning-title">
                {title ?? t('team.taskOverview')}
              </span>
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
        )}
        <ProgressSection
          tasks={allTasks}
          progressTasks={progressTasks}
          now={now}
          groupedTasks={groupedTasks}
          completedTasks={completedTasks}
          totalTasks={totalTasks}
          displayMode="count"
          members={members}
          hideAssignee={hideAssignee}
          statusIconAtEnd={statusIconAtEnd}
          renderTaskIcon={renderTaskIcon}
          maxCollapsedCount={maxCollapsedCount}
          expanded={expanded}
          emptyIllustration={emptyIllustration}
          workflowRuns={workflowRuns}
        />
      </div>
    );
  }

  const viewSwitcher = <ViewSwitcher view={view} onViewChange={setView} showWorkflow={workflowRuns.length > 0} />;

  const header = (
    <div className="flex h-8 items-center justify-between gap-3 my-6">
      <h2 className="text-sm font-semibold leading-5 text-text-strong" data-testid="team-area-task-planning-view-title">
        {t('team.planning.progressTitle')}
      </h2>
      {viewSwitcher}
    </div>
  );

  return (
    <div className="flex-1 overflow-hidden bg-card" data-testid="team-area-task-planning-panel" data-variant="expanded">
      {view === 'list' ? (
        <div className="flex h-full flex-col px-6 pb-6" data-testid="team-area-task-planning-list-view">
          {header}
          {workflowRuns.length > 0 && activeSessionId ? (
            <>
              <ProgressBar progressPercent={progressPercent} groupedTasks={groupedTasks} />
              <SwarmflowTreeView runs={workflowRuns} sessionId={activeSessionId} />
            </>
          ) : (
            <ProgressSection
              tasks={tasks}
              progressTasks={progressTasks}
              now={now}
              groupedTasks={groupedTasks}
              completedTasks={completedTasks}
              totalTasks={totalTasks}
              displayMode="percent"
              members={members}
              hideAssignee={hideAssignee}
              statusIconAtEnd={statusIconAtEnd}
              emptyIllustration={emptyIllustration}
            />
          )}
        </div>
      ) : view === 'workflow' ? (
        <div className="flex h-full flex-col px-6 pb-6" data-testid="team-area-task-planning-workflow-view">
          {header}
          <ProgressBar progressPercent={progressPercent} groupedTasks={groupedTasks} />
          {workflowRuns.length > 0 && activeSessionId ? (
            <SwarmflowGraphView runs={workflowRuns} sessionId={activeSessionId} />
          ) : null}
        </div>
      ) : (
        <div className="flex h-full flex-col px-6 pb-6">
          {header}
          <ProgressBar progressPercent={progressPercent} groupedTasks={groupedTasks} />

          <div
            className="min-h-0 flex-1 overflow-y-auto rounded-lg bg-secondary p-6"
            data-testid="team-area-task-planning-board"
          >
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
                />
              ))}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

function BoardColumn({
  column,
  tasks,
  members,
  hideAssignee,
}: {
  column: (typeof BOARD_COLUMNS)[number];
  tasks: SessionTeamTask[];
  members: TeamMember[];
  hideAssignee: boolean;
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
          return <BoardTaskCard key={task.task_id} task={task} members={members} hideAssignee={hideAssignee} />;
        })}
      </div>
    </section>
  );
}

function BoardTaskCard({
  task,
  members,
  hideAssignee,
}: {
  task: SessionTeamTask;
  members: TeamMember[];
  hideAssignee: boolean;
}) {
  const assigneeExists = Boolean(task.assignee && members.some((member) => member.member_id === task.assignee));
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
        <h3
          className="truncate text-base font-medium leading-[18px] text-text-strong"
          title={title}
          data-testid="team-area-task-planning-board-task-title"
        >
          {title}
        </h3>
        {content ? (
          <p
            className="mt-2 line-clamp-2 text-sm leading-5 text-text-muted"
            title={content}
            data-testid="team-area-task-planning-board-task-content"
          >
            {content}
          </p>
        ) : null}
        <TaskResourcePanel skills={task.skills} files={task.files} />
      </div>
      <div
        className="mt-3 flex h-8 items-center bg-[var(--color-task-card-surface)] px-1 pb-1"
        data-testid="team-area-task-planning-board-task-footer"
      >
        {!hideAssignee &&
          (assigneeExists ? (
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
          ))}
      </div>
    </article>
  );
}

function TaskResourcePanel({ skills, files }: { skills?: string[]; files?: string[] }) {
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
      <div
        className="flex h-6 items-center gap-4 border-b border-border"
        role="tablist"
        aria-label={t('team.planning.resources')}
        data-testid="team-area-task-planning-resources-tabs"
      >
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
            icon={
              resolvedActiveTab === 'skills' ? (
                <Puzzle className="h-4 w-4 shrink-0 text-muted" aria-hidden="true" />
              ) : (
                <File className="h-4 w-4 shrink-0 text-muted" aria-hidden="true" />
              )
            }
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
      <span className={active ? 'font-medium text-text-strong' : 'text-text'}>{label}</span>
      <span className="flex h-4 min-w-4 items-center justify-center rounded-full bg-secondary px-1 text-[10px] leading-4 text-text-strong">
        {count}
      </span>
      {active && <span className="absolute -bottom-px left-0 h-0.5 w-6 bg-text-strong" />}
    </button>
  );
}

function ResourceLine({ icon, label }: { icon: ReactNode; label: string }) {
  return (
    <div className="mb-2 flex items-center gap-1 text-xs text-text last:mb-0">
      {icon}
      <span className="truncate">{label}</span>
    </div>
  );
}
