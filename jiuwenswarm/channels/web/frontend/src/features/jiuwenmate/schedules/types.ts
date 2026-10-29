export type ScheduleKind = 'cron' | 'interval' | 'once';
export type ScheduleFrequency = 'daily' | 'weekly' | 'monthly' | 'custom';
export type ScheduleStatus = 'running' | 'paused' | 'failed' | 'completed' | 'queued';

export interface ScheduleDraft {
  name: string;
  prompt: string;
  cronExpr: string;
  timezone: string;
  scheduleKind: ScheduleKind;
  intervalSeconds: number | null;
  runAt: string | null;
  startsAt: string | null;
  endsAt: string | null;
}

export interface Schedule extends ScheduleDraft {
  id: string;
  agentId: string;
  createdAt: string;
  enabled: boolean;
  nextFireAt: string | null;
  conversationId: string;
  runCount: number;
  lastStatus: string;
}

export interface ScheduleRun {
  requestId: string;
  scheduledFor: string;
  status: string;
  description: string;
}
