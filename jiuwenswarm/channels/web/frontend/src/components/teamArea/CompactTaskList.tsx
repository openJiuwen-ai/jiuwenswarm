/**
 * CompactTaskList 组件
 *
 * 紧凑态任务列表，展示任务行的状态图标、负责人头像（或自定义图标）和标题。
 * 从 TaskPlanningPanel 的 compact 模式中抽离，供 team 集群模式和非集群模式复用。
 *
 * 入参：
 * - tasks:       任务列表（SessionTeamTask[]）
 * - members:     团队成员列表（TeamMember[]），用于匹配任务负责人
 * - hideAssignee:是否隐藏任务行负责人头像（非集群模式复用时传 true）
 * - renderTaskIcon?: 自定义状态图标与标题之间的内容（如人物图标），不传则按 hideAssignee 决定是否显示成员头像
 * - renderStatusIcon?: 自定义第一个状态图标，不传则按任务状态取默认图标
 * - statusIconAtEnd?:  状态图标放到行尾（标题之后），默认放在行首
 * - maxCollapsedCount?: 折叠态最大显示任务数（未传或 expanded 为 true 时不截断）
 * - expanded?:         是否已展开全部（默认 false）
 * - emptyText?:        空列表时显示的文本（不传则默认 t('common.noData')）
 * - emptyIllustration?:空列表时显示的插图 URL（不传则仅显示文本）
 *
 * 使用位置：
 * - TaskPlanningPanel.tsx compact 模式（team 集群模式 / 规划·性能模式复用）
 */
import type { ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { ApplicationTaskControls } from '../../applicationPlugins/ApplicationTaskControls';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import type { TeamTask as SessionTeamTask } from '../../stores/sessionStore';
import { LoadingSpinner } from '../ui/LoadingSpinner/LoadingSpinner';
import StatusSuccessIcon from '../../assets/work-mode/status-success.svg?react';
import FrameTimeIcon from '../../assets/work-mode/frame-time.svg?react';
import CancelledIcon from '../../assets/work-mode/已取消.svg?react';
import { UnassignedTeamAvatar } from './UnassignedTeamAvatar';
import { getBoardTaskTitle, getMemberDisplayName, getTaskColumnKey, type TeamMember } from './shared';
import { workflowAgentAvatarName, workflowAgentDisplayName, workflowAgentIdentity, workflowPhaseWorkers, type WorkflowAgent, type WorkflowRun } from './workflowTypes';
import { useEnsurePhaseAgents } from './useEnsurePhaseAgents';

export interface CompactTaskListProps {
  tasks: SessionTeamTask[];
  members: TeamMember[];
  hideAssignee: boolean;
  /** 镜像 workflow 任务的 run 数据：带 workflow_run_id 的任务据此渲染 worker 头像栈。 */
  workflowRuns?: WorkflowRun[];
  renderTaskIcon?: (task: SessionTeamTask) => ReactNode;
  renderStatusIcon?: (task: SessionTeamTask) => ReactNode;
  statusIconAtEnd?: boolean;
  maxCollapsedCount?: number;
  expanded?: boolean;
  emptyText?: string;
  emptyIllustration?: string;
  onTaskClick?: (taskId: string) => void;
}

export function CompactTaskList({
  tasks,
  members,
  hideAssignee,
  workflowRuns,
  renderTaskIcon,
  renderStatusIcon,
  statusIconAtEnd = false,
  maxCollapsedCount,
  expanded = false,
  emptyText,
  emptyIllustration,
  onTaskClick,
}: CompactTaskListProps) {
  const { t } = useTranslation();
  const visibleTasks = maxCollapsedCount !== undefined && !expanded ? tasks.slice(0, maxCollapsedCount) : tasks;
  // 必须在早退之前：空列表 ↔ 非空切换时 hook 数量变化会炸整棵树（React #310）
  useEnsurePhaseAgents(visibleTasks, workflowRuns);

  if (tasks.length === 0) {
    return (
      <div
        className="flex flex-col items-center justify-center gap-2 text-center text-sm text-text-muted"
        style={{ height: 120 }}
        data-testid="team-area-task-planning-empty"
      >
        {emptyIllustration && <img src={emptyIllustration} alt="" width={48} height={48} className="shrink-0" />}
        <span>{emptyText ?? t('common.noData')}</span>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-1">
      {visibleTasks.map(task => {
        const assigneeExists = Boolean(task.assignee && members.some(member => member.member_id === task.assignee));
        const assigneeName = getMemberDisplayName(task.assignee || '');
        const title = getBoardTaskTitle(task);
        const columnKey = getTaskColumnKey(task);
        // 镜像的 workflow phase 任务无 assignee：按 run 的 phase agents 渲染色块头像栈。
        const workers = task.workflow_run_id ? workflowPhaseWorkers(task, workflowRuns ?? []) : [];
        const statusIcon = renderStatusIcon ? (
          renderStatusIcon(task)
        ) : columnKey === 'running' ? (
          <span
            className="flex h-4 w-4 shrink-0 items-center justify-center overflow-hidden"
            data-testid="team-area-task-planning-task-status-icon"
          >
            <LoadingSpinner />
          </span>
        ) : columnKey === 'cancelled' ? (
          <span
            className="flex h-4 w-4 shrink-0 items-center justify-center overflow-hidden"
            data-testid="team-area-task-planning-task-status-icon"
          >
            <CancelledIcon className="h-4 w-4 shrink-0" aria-hidden="true" />
          </span>
        ) : columnKey === 'waiting' ? (
          <span
            className="flex h-4 w-4 shrink-0 items-center justify-center overflow-hidden"
            data-testid="team-area-task-planning-task-status-icon"
          >
            <FrameTimeIcon className="h-4 w-4 shrink-0 text-text-meta" aria-hidden="true" />
          </span>
        ) : (
          <span
            className="flex h-4 w-4 shrink-0 items-center justify-center overflow-hidden"
            data-testid="team-area-task-planning-task-status-icon"
          >
            <StatusSuccessIcon className="h-4 w-4 shrink-0 text-[var(--color-team-status-completed-icon)]" aria-hidden="true" />
          </span>
        );
        return (
          <div
            key={task.task_id}
            data-testid="team-area-task-planning-task-row"
            data-variant={task.task_id}
            className={`flex h-[38px] items-center gap-2 rounded-md ${onTaskClick ? 'cursor-pointer' : ''}`}
            onClick={onTaskClick ? () => onTaskClick(task.task_id) : undefined}
            role={onTaskClick ? 'button' : undefined}
            tabIndex={onTaskClick ? 0 : undefined}
            onKeyDown={
              onTaskClick
                ? e => {
                    if (e.key === 'Enter' || e.key === ' ') {
                      e.preventDefault();
                      onTaskClick(task.task_id);
                    }
                  }
                : undefined
            }
          >
            {!statusIconAtEnd && statusIcon}
            {renderTaskIcon
              ? renderTaskIcon(task)
              : !hideAssignee &&
                (workers.length > 0 ? (
                  <CompactWorkerStack workers={workers} />
                ) : assigneeExists ? (
                  <TeamMemberAvatar member={task.assignee} alt={assigneeName} className="h-4 w-4 rounded-full shrink-0" imageClassName="rounded-full" />
                ) : (
                  <UnassignedTeamAvatar className="h-4 w-4 rounded-full shrink-0" />
                ))}
            <ApplicationTaskControls taskId={task.task_id}>
              <span
                className="min-w-0 flex-1 text-sm leading-[22px] text-text truncate"
                data-testid="team-area-task-planning-task-title"
                title={title}
              >
                {title}
              </span>
            </ApplicationTaskControls>
            {statusIconAtEnd && statusIcon}
          </div>
        );
      })}
    </div>
  );
}

/**
 * 紧凑行内的重叠 worker 头像栈（最多 3 个 + "+N" 芯片），与工作流树视图同一套
 * 身份映射：三方节点认 CLI 品牌脸、人类节点认人类脸、native 按名字哈希取插画。
 * 栈的 title 列出全部 worker 名（含被 +N 折叠的）。
 */
function CompactWorkerStack({ workers }: { workers: WorkflowAgent[] }) {
  const visible = workers.slice(0, 3);
  const extra = workers.length - visible.length;
  const names = workers.map((worker) => workflowAgentDisplayName(worker)).join(', ');
  return (
    <div
      className="flex items-center shrink-0"
      title={names}
      data-testid="team-area-task-planning-task-worker-stack"
    >
      {visible.map((worker, index) => (
        <div
          key={worker.id}
          className={index > 0 ? '-ml-1.5 rounded-full ring-2 ring-[var(--color-surface-card)]' : 'rounded-full'}
        >
          <TeamMemberAvatar
            member={workflowAgentAvatarName(worker)}
            identity={workflowAgentIdentity(worker)}
            alt={workflowAgentAvatarName(worker)}
            className="h-4 w-4 rounded-full"
            imageClassName="rounded-full"
          />
        </div>
      ))}
      {extra > 0 && (
        <span className="-ml-1.5 flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-secondary text-[9px] leading-none text-text-muted ring-2 ring-[var(--color-surface-card)]">
          +{extra}
        </span>
      )}
    </div>
  );
}
