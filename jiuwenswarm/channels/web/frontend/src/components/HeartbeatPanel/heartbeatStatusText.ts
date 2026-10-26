import type { HeartbeatJobStatus, HeartbeatRunStatus } from '../../types/heartbeat';

export type HeartbeatStatusVariant = 'running' | 'scheduled' | 'paused' | 'completed' | 'expired';

/** 状态展示口径完全来自服务端 status 字段，不由 enabled 反推，见接口规格说明 §10.12 */
export function heartbeatStatusVariant(status: HeartbeatJobStatus): HeartbeatStatusVariant {
  switch (status) {
    case 'running':
      return 'running';
    case 'scheduled':
      return 'scheduled';
    case 'disabled':
      return 'paused';
    case 'completed':
      return 'completed';
    case 'expired':
      return 'expired';
    default:
      return 'paused';
  }
}

export function heartbeatStatusLabelKey(status: HeartbeatJobStatus): string {
  return `heartbeat.status.${heartbeatStatusVariant(status)}`;
}

/**
 * "立即运行"按钮是否可点：只有 enabled 且服务端状态为 scheduled 时才允许，
 * running/disabled/completed/expired 一律禁用（见接口交接文档 §2.3）。
 * actingJobId 命中（面板正在发起别的操作）不在这里判断——那是局部 UI 状态，
 * 由调用方自己在这个结果基础上再 && 一层。
 */
export function canHeartbeatRunNow(enabled: boolean, status: HeartbeatJobStatus): boolean {
  return enabled && status === 'scheduled';
}

const KNOWN_RUN_NOW_REJECT_REASONS = [
  'session_missing',
  'session_busy',
  'previous_run_active',
  'already_queued',
  'replacement_pending',
  'replacement_cancel_failed',
  'job_disabled_during_replace',
  'job_completed', // §6：Once/delete_after_run/max_runs 已满足停止条件，需禁用并提示先恢复任务
];

/**
 * run_now 结果 -> 文案 key。accepted=true 才能提示"已接收/已开始执行"，绝不显示"执行成功"；
 * accepted=false 按 reason 显示具体原因，不显示"RPC 失败"，见接口规格说明 §16.7。
 */
export function heartbeatRunNowMessageKey(accepted: boolean, reason?: string, queued?: boolean): string {
  if (accepted) {
    return queued ? 'heartbeat.toast.runNowQueued' : 'heartbeat.toast.runNowAccepted';
  }
  const key = reason && KNOWN_RUN_NOW_REJECT_REASONS.includes(reason) ? reason : 'unknown';
  return `heartbeat.toast.runNowRejected.${key}`;
}

const KNOWN_CANCEL_STATUSES = ['idle', 'cancelled', 'not_found', 'failed'];

/** cancel_status -> 文案 key；not_found 不能显示"取消成功"，见接口规格说明 §16.7 */
export function heartbeatCancelMessageKey(cancelStatus: string): string {
  const key = KNOWN_CANCEL_STATUSES.includes(cancelStatus) ? cancelStatus : 'failed';
  return `heartbeat.toast.cancel.${key}`;
}

export function heartbeatLastRunStatusLabelKey(status: HeartbeatRunStatus | null): string | null {
  if (!status) return null;
  return `heartbeat.runState.${status}`;
}
