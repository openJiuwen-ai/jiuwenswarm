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
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import type { TeamTask as SessionTeamTask } from '../../stores/sessionStore';
import statusProcessingIcon from '../../assets/work-mode/status-processing.svg';
import statusSuccessIcon from '../../assets/work-mode/status-success.svg';
import statusWaitingIcon from '../../assets/work-mode/status-waiting.svg';
import statusWarningIcon from '../../assets/work-mode/status-warning.svg';
import teamLeaderIcon from '../../assets/teamleader.svg';
import { getBoardTaskTitle, getMemberDisplayName, getTaskColumnKey, type TaskColumnKey, type TeamMember } from './shared';

const compactStatusIcons: Record<TaskColumnKey, string> = {
  completed: statusSuccessIcon,
  running: statusProcessingIcon,
  waiting: statusWaitingIcon,
  cancelled: statusWarningIcon,
};

export interface CompactTaskListProps {
  tasks: SessionTeamTask[];
  members: TeamMember[];
  hideAssignee: boolean;
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

  const visibleTasks = maxCollapsedCount !== undefined && !expanded ? tasks.slice(0, maxCollapsedCount) : tasks;

  return (
    <div className="flex flex-col gap-1">
      {visibleTasks.map(task => {
        const assigneeExists = Boolean(task.assignee && members.some(member => member.member_id === task.assignee));
        const assigneeName = getMemberDisplayName(task.assignee || '');
        const title = getBoardTaskTitle(task);
        const columnKey = getTaskColumnKey(task);
        const statusIcon = renderStatusIcon ? (
          renderStatusIcon(task)
        ) : (
          <img
            src={compactStatusIcons[columnKey]}
            className={`h-4 w-4 shrink-0 ${columnKey === 'running' ? 'animate-spin' : ''}`}
            aria-hidden="true"
            data-testid="team-area-task-planning-task-status-icon"
          />
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
                (assigneeExists ? (
                  <TeamMemberAvatar member={task.assignee} alt={assigneeName} className="h-4 w-4 rounded-full shrink-0" imageClassName="rounded-full" />
                ) : (
                  <UnassignedTeamAvatar className="h-4 w-4 rounded-full shrink-0" />
                ))}
            <span className="flex-1 text-sm leading-[22px] text-text truncate" data-testid="team-area-task-planning-task-title">
              {title}
            </span>
            {statusIconAtEnd && statusIcon}
          </div>
        );
      })}
    </div>
  );
}

function UnassignedTeamAvatar({ className }: { className?: string }) {
  return (
    <img
      src={teamLeaderIcon}
      className={className}
      alt=""
      aria-hidden="true"
    />
  );
}
