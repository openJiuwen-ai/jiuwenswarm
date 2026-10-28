import { useTranslation } from 'react-i18next';
import { workflowStatusIcon, type WorkflowStatus } from './workflowTypes';

/**
 * SwarmFlow run 分组组头：workflow 名 + 截断 run id + 状态徽章。
 * 成员列表、聊天活动卡、任务面板三处共用，保持组头形态一致。
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

export function RunGroupHeader({ name, shortId, status, runId }: {
  name: string;
  shortId: string;
  status: string;
  /** 完整 run id，用作短 id 的悬浮提示（面板上通常没有别处可见）。 */
  runId?: string;
}) {
  const { t } = useTranslation();
  const statusKey = STATUS_KEY[status];
  const pillClass = STATUS_PILL_CLASS[status] ?? 'bg-gray-500/10 text-gray-500';
  return (
    <div
      className="flex min-w-0 items-center gap-1.5 py-1"
      data-testid="team-area-run-group-header"
      data-variant={runId ?? ''}
    >
      <span className="min-w-0 truncate text-xs font-semibold text-text" data-testid="team-area-run-group-header-name">
        {name}
      </span>
      <span
        className="shrink-0 text-[10px] text-text-muted"
        title={runId ? t('swarmflow.runIdTooltip', { id: runId }) : undefined}
        data-testid="team-area-run-group-header-short-id"
      >
        #{shortId}
      </span>
      {statusKey && (
        <span
          className={`shrink-0 rounded-full px-1.5 py-px text-[10px] leading-4 ${pillClass}`}
          data-testid="team-area-run-group-header-status"
          data-variant={status}
        >
          {/* statusKey 只含 8 个已知状态，此时 status 必为合法 WorkflowStatus */}
          {workflowStatusIcon(status as WorkflowStatus)} {t(statusKey)}
        </span>
      )}
    </div>
  );
}
