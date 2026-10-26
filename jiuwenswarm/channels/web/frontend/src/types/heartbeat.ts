// src/types/heartbeat.ts
/** 后端 heartbeat.job.* 系列 RPC 实际收发的字段，对齐 jiuwenswarm/gateway/heartbeat/models.py 的 HeartbeatJob.to_dict() */

export type HeartbeatJobStatus = 'scheduled' | 'running' | 'completed' | 'expired' | 'disabled';
export type HeartbeatRunStatus = 'succeeded' | 'failed' | 'skipped' | 'cancelled';
export type HeartbeatConcurrencyPolicy = 'skip' | 'queue' | 'replace';
export type HeartbeatSessionDeletedPolicy = 'disable' | 'completed';
export type HeartbeatScheduleKind = 'interval' | 'cron' | 'once';

export type HeartbeatScheduleDTO =
  | { type: 'interval'; interval_seconds: number }
  | { type: 'cron'; cron_expr: string; timezone: string }
  | { type: 'once'; run_at: number };

/**
 * Heartbeat 自动触发的执行身份标记。后端在自动触发时把它塞进
 * `payload.metadata.automation`（见 scheduler.py 的 _build_message），
 * 并随 user/assistant 历史一起落盘，刷新/切会话/后端重启后仍能识别同一条自动轮。
 * 前端实时事件（chat.delta/final/error、processing_status、execution.error）和
 * 历史恢复（historyRestore.ts）必须共用同一个提取逻辑读取它，而不是各自重判。
 * 对齐「心跳任务前端开发与接口规格说明2」§7。
 */
export interface HeartbeatAutomationMetadata {
  kind: 'heartbeat';
  job_id: string;
  run_id: string;
  triggered_at: number; // Unix 秒
  source: 'agent_tool' | 'web_rpc' | 'tui_rpc' | 'schedule_recovery';
  trigger: 'scheduler' | 'run_now';
}

export interface HeartbeatRunState {
  current_run_id: string | null;
  current_run_started_at: number | null;
  last_run_status: HeartbeatRunStatus | null;
  last_error: string | null;
  last_cancel_status: string | null;
  last_cancel_error: string | null;
  queued_run_id: string | null;
  queued_trigger: string | null;
  queued_reschedule: boolean;
  current_trigger: string | null;
  current_reschedule: boolean;
  resume_status: string | null;
  resume_enabled: boolean | null;
  resume_next_run_at: number | null;
  skipped_count: number;
}

export interface HeartbeatJobDTO {
  id: string;
  kind: 'heartbeat';
  name: string;
  enabled: boolean;
  status: HeartbeatJobStatus;
  channel_id: string;
  session_id: string;
  prompt: string;
  schedule: HeartbeatScheduleDTO;
  timezone: string;
  concurrency_policy: HeartbeatConcurrencyPolicy;
  session_deleted_policy: HeartbeatSessionDeletedPolicy;
  max_runs: number | null;
  delete_after_run: boolean;
  created_at: number | null;
  updated_at: number | null;
  next_run_at: number | null;
  last_run_at: number | null;
  run_count: number;
  metadata: { source: string; [key: string]: unknown };
  run_state: HeartbeatRunState;
}

/** UI 层展示用结构，来自 HeartbeatJobDTO 派生，见 HeartbeatPanel/index.tsx 的 heartbeatJobToUI */
export interface HeartbeatTaskUI {
  id: string;
  name: string;
  prompt: string;
  enabled: boolean;
  status: HeartbeatJobStatus;
  schedule: HeartbeatScheduleDTO;
  timezone: string;
  concurrencyPolicy: HeartbeatConcurrencyPolicy;
  sessionDeletedPolicy: HeartbeatSessionDeletedPolicy;
  maxRuns: number | null;
  createdAt: number | null;
  updatedAt: number | null;
  nextRunAt: number | null;
  lastRunAt: number | null;
  runCount: number;
  runState: HeartbeatRunState;
}

export interface HeartbeatMeta {
  limits: {
    min_interval_seconds: number;
    max_active_jobs_per_session: number;
    max_active_jobs_global: number;
    default_max_runs: number;
    default_concurrency_policy: HeartbeatConcurrencyPolicy;
    default_session_deleted_policy: HeartbeatSessionDeletedPolicy;
  };
  schedule_types: HeartbeatScheduleKind[];
  concurrency_policies: HeartbeatConcurrencyPolicy[];
  session_deleted_policies: HeartbeatSessionDeletedPolicy[];
  statuses: HeartbeatJobStatus[];
  sources: string[];
  run_count_semantics: string;
  deprecated_fields: Record<string, string>;
}

export interface HeartbeatRunNowResult {
  accepted: boolean;
  run_id: string;
  session_id?: string;
  queued?: boolean;
  reason?:
    | 'session_missing'
    | 'session_busy'
    | 'previous_run_active'
    | 'already_queued'
    | 'replacement_pending'
    | 'replacement_cancel_failed'
    | 'job_disabled_during_replace'
    | 'job_completed'; // §6：Once/delete_after_run/max_runs 已满足停止条件，前端需禁用并提示先恢复任务
}

export type HeartbeatCancelStatus = 'idle' | 'cancelled' | 'not_found' | 'failed';

export interface HeartbeatCancelResult {
  job_id: string;
  cancelled_run_id: string | null;
  cancel_status: HeartbeatCancelStatus;
  paused: boolean;
}

export interface HeartbeatPreviewItem {
  run_at: number;
  iso: string;
}
