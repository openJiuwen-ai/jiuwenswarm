export interface LongHorizonAnchor {
  type?: string;
  month: number;
  day?: number | null;
  timezone?: string;
}

export interface LongHorizonStage {
  id: string;
  offset_days: number;
  title: string;
  hint?: string;
  plan?: string;
  status: string;
  due_at?: string;
  cron_job_id?: string;
  snooze_until?: string;
  conclusion?: string;
}

export interface LongHorizonTask {
  id: string;
  title: string;
  kind: string;
  anchor: LongHorizonAnchor;
  recurrence: string;
  status: string;
  exec_session_id: string;
  created_at?: string;
  updated_at?: string;
  brief?: string;
  stages: LongHorizonStage[];
}

export interface LongHorizonInboxItem {
  task: LongHorizonTask;
  stage: LongHorizonStage;
}

export interface LongHorizonReminderToast {
  id: string;
  taskId: string;
  stageId: string;
  execSessionId?: string;
  title: string;
  stageTitle: string;
  hint?: string;
  dueAt?: string;
}
