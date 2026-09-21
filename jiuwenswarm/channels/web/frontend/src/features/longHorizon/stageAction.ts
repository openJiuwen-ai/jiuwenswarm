import { dismissReminder } from './reminderState';
import { requestOpenSession, type OpenSessionDetail } from './sessionNav';

export const DEFAULT_SNOOZE_HOURS = 2;

export type LongHorizonStageActionName = 'start' | 'snooze' | 'skip';

type StageActionRequest = <T = unknown>(
  method: string,
  params?: Record<string, unknown>
) => Promise<T>;

type OpenSessionFn = (detail: OpenSessionDetail) => void;

async function defaultRequest<T = unknown>(
  method: string,
  params?: Record<string, unknown>
): Promise<T> {
  const { webRequest } = await import('../../services/webClient');
  return webRequest<T>(method, params);
}

export async function runLongHorizonStageAction(
  params: {
    taskId: string;
    stageId: string;
    action: LongHorizonStageActionName;
    execSessionId?: string;
    title?: string;
  },
  deps?: {
    request?: StageActionRequest;
    openSession?: OpenSessionFn;
  }
): Promise<void> {
  const request = deps?.request ?? defaultRequest;
  const openSession = deps?.openSession ?? requestOpenSession;
  const result = await request<{
    exec_session_id?: string;
    kick_query?: string;
    success?: boolean;
  }>('long_horizon_stage_action', {
    task_id: params.taskId,
    stage_id: params.stageId,
    stage_action: params.action,
    ...(params.action === 'snooze' ? { snooze_hours: DEFAULT_SNOOZE_HOURS } : {}),
  });

  dismissReminder(`${params.taskId}:${params.stageId}`);

  if (params.action === 'start') {
    const sid = result.exec_session_id || params.execSessionId;
    if (sid) {
      openSession({
        sessionId: sid,
        title: params.title,
        kickQuery: result.kick_query,
        taskId: params.taskId,
        stageId: params.stageId,
      });
    }
  }
}
