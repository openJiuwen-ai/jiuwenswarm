import { useTranslation } from 'react-i18next';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { TeamMemberAvatar } from '../TeamMemberAvatar';
import { workflowStatusIcon, type WorkflowStatus } from './workflowTypes';

/**
 * SwarmFlow run group header: workflow name + short run id + status badge +
 * executor badge. Shared by the members panel, chat activity card and task
 * panel to keep the group header shape consistent.
 */

const STATUS_KEY: Record<string, string> = {
  planned: 'swarmflow.statusPlanned',
  pending: 'swarmflow.statusPending',
  running: 'swarmflow.statusRunning',
  paused: 'swarmflow.statusPaused',
  completed: 'swarmflow.statusCompleted',
  failed: 'swarmflow.statusFailed',
  stopped: 'swarmflow.statusStopped',
  waiting_for_human: 'swarmflow.statusWaitingForHuman',
};

const STATUS_PILL_CLASS: Record<string, string> = {
  completed: 'bg-emerald-500/10 text-emerald-500',
  running: 'bg-blue-500/10 text-blue-500',
  paused: 'bg-violet-500/10 text-violet-500',
  failed: 'bg-red-500/10 text-red-500',
  waiting_for_human: 'bg-amber-500/10 text-amber-500',
  stopped: 'bg-gray-500/10 text-gray-500',
  planned: 'bg-gray-500/10 text-gray-500',
  pending: 'bg-gray-500/10 text-gray-500',
};

export function RunGroupHeader({ name, shortId, status, runId, executor, collapsed, onToggle }: {
  name: string;
  shortId: string;
  status: string;
  /** 完整 run id，用作短 id 的悬浮提示（面板上通常没有别处可见）。 */
  runId?: string;
  /** Member currently hosting the run (leader included). null/missing = no
   * team context, no badge rendered. */
  executor?: string | null;
  /** 折叠态；给了 ``onToggle`` 才渲染折叠按钮（其它展示面不需要折叠）。 */
  collapsed?: boolean;
  onToggle?: () => void;
}) {
  const { t } = useTranslation();
  const statusKey = STATUS_KEY[status];
  const pillClass = STATUS_PILL_CLASS[status] ?? 'bg-gray-500/10 text-gray-500';
  // 两行布局，第一行只放"脚本名 + run id"，第二行只放"执行者 + 状态"：
  // 单行时 240px 侧栏里 #runId / 执行者 / 状态三个不可压缩元素会把
  // ``min-w-0 truncate`` 的名字压成 0 宽、状态被右边界裁成半截。
  // 第二行里执行者 pill 可压缩（min-w-0 + truncate），状态 pill 永远完整。
  return (
    <div
      className="flex min-w-0 flex-col gap-0.5 py-1"
      data-testid="team-area-run-group-header"
      data-variant={runId ?? ''}
    >
      <div className="flex min-w-0 items-center gap-1">
        {onToggle && (
          <button
            type="button"
            className="shrink-0 rounded p-0.5 text-text-muted hover:bg-secondary hover:text-text"
            title={collapsed ? t('team.expand') : t('team.collapse')}
            aria-expanded={!collapsed}
            data-testid="team-area-run-group-toggle"
            onClick={(event) => {
              event.stopPropagation();
              onToggle();
            }}
          >
            {collapsed ? <ChevronRight className="h-3 w-3" /> : <ChevronDown className="h-3 w-3" />}
          </button>
        )}
        <span
          className="min-w-0 flex-1 truncate text-xs font-semibold text-text"
          title={name}
          data-testid="team-area-run-group-header-name"
        >
          {name}
        </span>
        <span
          className="shrink-0 text-[10px] text-text-muted"
          title={runId ? t('swarmflow.runIdTooltip', { id: runId }) : undefined}
          data-testid="team-area-run-group-header-short-id"
        >
          #{shortId}
        </span>
      </div>
      <div className={`flex min-w-0 items-center gap-1.5 ${onToggle ? 'pl-5' : ''}`}>
        {executor && (
          <span
            className="flex min-w-0 items-center gap-1 rounded-full bg-primary/10 px-1.5 py-px text-[10px] text-text-muted"
            title={t('swarmflow.executorTooltip', { name: executor })}
            data-testid="team-area-run-group-header-executor"
          >
            <TeamMemberAvatar
              member={executor}
              className="h-4 w-4 shrink-0 rounded-full"
              imageClassName="rounded-full"
              alt={executor}
            />
            <span className="min-w-0 truncate">{executor}</span>
          </span>
        )}
        {statusKey && (
          <span
            className={`ml-auto shrink-0 rounded-full px-1.5 py-px text-[10px] leading-4 ${pillClass}`}
            data-testid="team-area-run-group-header-status"
            data-variant={status}
          >
            {/* statusKey 只含 8 个已知状态，此时 status 必为合法 WorkflowStatus */}
            {workflowStatusIcon(status as WorkflowStatus)} {t(statusKey)}
          </span>
        )}
      </div>
    </div>
  );
}
