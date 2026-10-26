/**
 * MemberTaskDrawer 组件 - 成员任务详情抽屉
 *
 * 显示选中成员的任务列表，支持折叠展开
 */

import { useState, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import { useChatStore, useTodoStore, useSessionStore } from '../../stores';
import { TeamMemberAvatar } from '../TeamMemberAvatar';

interface MemberTaskDrawerProps {
  memberId: string;
  onClose: () => void;
}

// 获取成员显示名称
const getMemberDisplayName = (memberId: string): string => {
  return memberId.replace(/_/g, ' ').replace(/\b\w/g, l => l.toUpperCase());
};

// 获取成员角色描述
const getMemberRole = (memberId: string): string => {
  const roles: Record<string, string> = {
    'ethan': '宏观与监管环境分析师',
    'claire': '信用评估专家',
    'eric': '反欺诈专家',
    'lily': '审批流程设计师',
    'david': '数据治理专家',
  };
  const key = memberId.toLowerCase().split('_')[0];
  return roles[key] || '智能体专家';
};

// 折叠/展开图标 - 右上左下箭头
const ChevronIcon = ({ expanded }: { expanded: boolean }) => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    className={`  ${expanded ? '' : '-rotate-90'}`}
  >
    <path d="M18 15l-6-6-6 6" />
  </svg>
);

// 任务状态图标
const TaskStatusIcon = ({ status }: { status: string }) => {
  switch (status) {
    case 'completed':
      return (
        <svg className="w-4 h-4 text-green-500" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M5 13l4 4L19 7" />
        </svg>
      );
    // Running family: execution plus the planning / in_review gates.
    case 'in_progress':
    case 'planning':
    case 'in_review':
      return (
        <svg className="w-4 h-4 text-blue-500 animate-spin" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M4 4v5h.582m15.356 2A8.001 8.001 0 004.582 9m0 0H9m11 11v-5h-.581m0 0a8.003 8.003 0 01-15.357-2m15.357 2H15" />
        </svg>
      );
    case 'pending':
    case 'queued':
      return (
        <svg className="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      );
    default:
      return (
        <svg className="w-4 h-4 text-gray-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
        </svg>
      );
  }
};

// 可折叠任务组组件
interface CollapsibleTaskGroupProps {
  title: string;
  count: number;
  expanded: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}

function CollapsibleTaskGroup({ title, count, expanded, onToggle, children }: CollapsibleTaskGroupProps) {
  return (
    <div className="border-b border-border bg-card">
      {/* 标题栏 */}
      <button
        onClick={onToggle}
        className="w-full flex items-center justify-between px-4 py-3 bg-card hover:bg-gray-50 "
      >
        <div className="flex items-center gap-2">
          <h3 className="text-sm font-medium text-text">{title}</h3>
          <span className="text-xs text-text-muted bg-secondary px-1.5 py-0.5 rounded">{count}</span>
        </div>
        <span className="text-text-muted">
          <ChevronIcon expanded={expanded} />
        </span>
      </button>
      {/* 分割线 */}
      <div className="border-t border-border" />
      {/* 内容区域 */}
      {expanded && (
        <div className="px-4 py-3 bg-card space-y-3">
          {children}
        </div>
      )}
    </div>
  );
}

export function MemberTaskDrawer({ memberId, onClose }: MemberTaskDrawerProps) {
  const { t } = useTranslation();
  const activeSessionId = useChatStore((s) => s.activeSessionId);
  const todos = useTodoStore((s) => s.runtimes[activeSessionId ?? '']?.todos ?? []);
  const teamTaskEvents = useSessionStore((s) => s.runtimes[activeSessionId ?? '']?.teamTaskEvents ?? []);

  const [inProgressExpanded, setInProgressExpanded] = useState(true);
  const [completedExpanded, setCompletedExpanded] = useState(false);
  const [pendingExpanded, setPendingExpanded] = useState(false);

  const displayName = getMemberDisplayName(memberId);
  const role = getMemberRole(memberId);

  // 获取该成员的任务列表
  const memberTasks = useMemo(() => {
    const taskMap = new Map();

    // 从 todos 获取任务
    todos.forEach((todo) => {
      if (todo.claimedBy === memberId) {
        taskMap.set(todo.id, {
          id: todo.id,
          title: todo.content || `任务 ${todo.id.slice(-4)}`,
          status: todo.status,
        });
      }
    });

    // 从 teamTaskEvents 补充任务
    teamTaskEvents.forEach((event) => {
      if (event.task_id && !taskMap.has(event.task_id)) {
        taskMap.set(event.task_id, {
          id: event.task_id,
          title: `任务 ${event.task_id.slice(-4)}`,
          status: event.status,
        });
      }
    });

    return Array.from(taskMap.values());
  }, [todos, teamTaskEvents, memberId]);

  // 按状态分组：planning / in_review 两个门控态归入"进行中"
  const inProgressStatuses = new Set(['in_progress', 'planning', 'in_review']);
  const inProgressTasks = memberTasks.filter(t => inProgressStatuses.has(t.status));
  const completedTasks = memberTasks.filter(t => t.status === 'completed');
  const pendingTasks = memberTasks.filter(t => t.status === 'pending' || t.status === 'queued');

  const renderTaskItem = (task: { id: string; title: string; status: string }) => (
    <div key={task.id} className="flex items-center gap-3 py-2">
      <span className="flex-shrink-0">
        <TaskStatusIcon status={task.status} />
      </span>
      <span className="text-sm text-text truncate flex-1">{task.title}</span>
    </div>
  );

  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      {/* 遮罩层 */}
      <div
        className="absolute inset-0 bg-black/30 backdrop-blur-sm"
        onClick={onClose}
      />
      {/* 抽屉面板 */}
      <div className="relative w-[420px] max-w-full h-full bg-bg shadow-xl flex flex-col animate-slide-in-right">
        {/* 头部 */}
        <div className="flex items-center justify-between px-4 py-3 border-b border-border bg-card">
          <div className="flex items-center gap-3">
            <TeamMemberAvatar
              member={memberId}
              alt={displayName}
              className="h-10 w-10 rounded-full"
              imageClassName="rounded-full"
            />
            <div>
              <h2 className="text-sm font-medium text-text">{displayName}</h2>
              <p className="text-xs text-text-muted">{role}</p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-1.5 rounded-lg hover:bg-secondary/50 "
          >
            <svg className="w-5 h-5 text-text-muted" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={2}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M6 18L18 6M6 6l12 12" />
            </svg>
          </button>
        </div>

        {/* 任务统计 */}
        <div className="px-4 py-3 bg-card border-b border-border">
          <div className="grid grid-cols-3 gap-4 text-center">
            <div>
              <div className="text-lg font-semibold text-text">{inProgressTasks.length}</div>
              <div className="text-[10px] text-text-muted">{t('team.inProgress')}</div>
            </div>
            <div>
              <div className="text-lg font-semibold text-text">{completedTasks.length}</div>
              <div className="text-[10px] text-text-muted">{t('team.completed')}</div>
            </div>
            <div>
              <div className="text-lg font-semibold text-text">{pendingTasks.length}</div>
              <div className="text-[10px] text-text-muted">{t('team.pending')}</div>
            </div>
          </div>
        </div>

        {/* 任务列表 */}
        <div className="flex-1 overflow-y-auto">
          {/* 进行中任务 - 默认展开 */}
          {inProgressTasks.length > 0 && (
            <CollapsibleTaskGroup
              title={t('team.inProgress')}
              count={inProgressTasks.length}
              expanded={inProgressExpanded}
              onToggle={() => setInProgressExpanded(!inProgressExpanded)}
            >
              {inProgressTasks.map(renderTaskItem)}
            </CollapsibleTaskGroup>
          )}

          {/* 已完成任务 - 默认收起 */}
          {completedTasks.length > 0 && (
            <CollapsibleTaskGroup
              title={t('team.completed')}
              count={completedTasks.length}
              expanded={completedExpanded}
              onToggle={() => setCompletedExpanded(!completedExpanded)}
            >
              {completedTasks.map(renderTaskItem)}
            </CollapsibleTaskGroup>
          )}

          {/* 待处理任务 - 默认收起 */}
          {pendingTasks.length > 0 && (
            <CollapsibleTaskGroup
              title={t('team.pending')}
              count={pendingTasks.length}
              expanded={pendingExpanded}
              onToggle={() => setPendingExpanded(!pendingExpanded)}
            >
              {pendingTasks.map(renderTaskItem)}
            </CollapsibleTaskGroup>
          )}

          {/* 无任务提示 */}
          {memberTasks.length === 0 && (
            <div className="flex flex-col items-center justify-center py-12 text-text-muted">
              <svg className="w-10 h-10 opacity-30 mb-3" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1}>
                <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
              </svg>
              <p className="text-sm">{t('team.noTasks')}</p>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
