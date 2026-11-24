import { useEffect } from 'react';
import { useChatStore } from '../../stores/chatStore';
import { useSessionStore } from '../../stores/sessionStore';
import type { TeamTask } from '../../stores/sessionStore';
import type { WorkflowRun } from './workflowTypes';
import { getExecutionProgress, getTaskEstimateLimit } from './taskProgress';
import { useTweenedPercent } from './useTweenedPercent';
import { useOrganizationConversationKey } from '../../features/teamOrganization/conversation';

const EMPTY_ESTIMATES: Record<string, number> = {};

/** Keep estimated ordinary-task progress separate from factual completion counts. */
export function useExecutionProgress(
  tasks: TeamTask[],
  runs: WorkflowRun[],
  sessionActive: boolean,
  sessionPaused: boolean,
): { percent: number; estimated: boolean } {
  const conversationId = useOrganizationConversationKey(useChatStore((s) => s.activeSessionId));
  // The session owns estimates so remounting a view cannot reset running work.
  const estimates = useSessionStore((s) => s.runtimes[conversationId]?.taskProgressEstimates ?? EMPTY_ESTIMATES);
  const candidatesKey = JSON.stringify(tasks
    .filter((task) => getTaskEstimateLimit(task) > 0)
    .map((task) => ({
      id: task.task_id,
      limit: getTaskEstimateLimit(task),
      active: sessionActive || (task.status === 'in_progress' && task.task_id.startsWith('application:')),
    })));

  useEffect(() => {
    const store = useSessionStore.getState();
    const saved = store.runtimes[conversationId]?.taskProgressEstimates ?? EMPTY_ESTIMATES;
    const candidates: { id: string; limit: number; active: boolean }[] = JSON.parse(candidatesKey);
    let current = Object.fromEntries(candidates.map(({ id, limit }) => [
      id, Math.min(limit, saved[id] ?? 0),
    ]));
    // Drop finished tasks so a retry with the same ID starts a fresh estimate.
    store.setTaskProgressEstimates(conversationId, current);
    const active = candidates.filter((task) => task.active);
    if (sessionPaused || !active.length || active.every(({ id, limit }) => current[id] >= limit)) return;
    const timer = window.setInterval(() => {
      const runtime = useSessionStore.getState().runtimes[conversationId];
      if (!runtime) {
        window.clearInterval(timer);
        return;
      }
      current = { ...runtime.taskProgressEstimates };
      for (const { id, limit } of active) current[id] = Math.min(limit, (current[id] ?? 0) + 0.01);
      store.setTaskProgressEstimates(conversationId, current);
      if (active.every(({ id, limit }) => current[id] >= limit)) window.clearInterval(timer);
    }, 1000);
    return () => window.clearInterval(timer);
  }, [candidatesKey, conversationId, sessionPaused]);

  const progress = getExecutionProgress(tasks, runs, estimates);
  const percent = useTweenedPercent(progress.percent);
  return { percent, estimated: progress.estimated };
}
