/**
 * TeamArea utilities - task planning metrics hook.
 */

import { useEffect, useMemo, useState } from 'react';
import { useChatStore, useSessionStore, useTodoStore } from '../../stores';
import { normalizeTaskStatus } from './shared';
import { getTasksForCurrentProgress } from '../../features/teamTaskProgressBaseline';
import { useOrganizationConversationKey } from '../../features/teamOrganization/conversation';

export function useTaskPlanningMetrics() {
  const activeSessionId = useOrganizationConversationKey(useChatStore(s => s.activeSessionId));
  const todos = useTodoStore(s => s.runtimes[activeSessionId ?? '']?.todos ?? []);
  const teamTaskEvents = useSessionStore(s => s.runtimes[activeSessionId ?? '']?.teamTaskEvents ?? []);
  const teamTasks = useSessionStore(s => s.runtimes[activeSessionId ?? '']?.teamTasks ?? []);
  const taskProgressBaseline = useSessionStore(s => s.runtimes[activeSessionId ?? '']?.teamTaskProgressBaseline);
  const progressTasks = useMemo(
    () => (taskProgressBaseline ? getTasksForCurrentProgress(teamTasks, taskProgressBaseline) : teamTasks),
    [taskProgressBaseline, teamTasks],
  );

  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 3_000);
    return () => window.clearInterval(timer);
  }, []);

  const totalTasks = useMemo(() => {
    if (teamTasks.length > 0) return teamTasks.length;
    const taskIds = new Set<string>();
    todos.forEach(todo => taskIds.add(todo.id));
    teamTaskEvents.forEach(event => {
      if (event.task_id) taskIds.add(event.task_id);
    });
    return taskIds.size;
  }, [teamTaskEvents, teamTasks.length, todos]);

  const completedTasks = useMemo(() => {
    if (teamTasks.length > 0) {
      return teamTasks.filter(task => task.status === 'completed').length;
    }
    const completed = new Set<string>();
    todos.forEach(todo => {
      if (normalizeTaskStatus(todo.status) === 'completed') completed.add(todo.id);
    });
    teamTaskEvents.forEach(event => {
      if (event.task_id && normalizeTaskStatus(event.status, event.type) === 'completed') {
        completed.add(event.task_id);
      }
    });
    return completed.size;
  }, [teamTaskEvents, teamTasks, todos]);

  return { completedTasks, progressTasks, teamTasks, totalTasks, now };
}
