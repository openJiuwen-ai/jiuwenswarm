import type { TeamTask } from '../stores/sessionStore';

export interface TaskProgressBaseline {
  confirmedTaskIds: string[];
  excludedCompletedTaskIds: string[];
}

export function createTaskProgressBaseline(): TaskProgressBaseline {
  return {
    confirmedTaskIds: [],
    excludedCompletedTaskIds: [],
  };
}

export function registerConfirmedTaskCreation(
  tasks: Pick<TeamTask, 'task_id' | 'status'>[],
  baseline: TaskProgressBaseline,
  taskId: string
): TaskProgressBaseline {
  if (baseline.confirmedTaskIds.includes(taskId)) {
    return baseline;
  }
  return {
    confirmedTaskIds: [...baseline.confirmedTaskIds, taskId],
    excludedCompletedTaskIds: tasks.filter(task => task.status === 'completed').map(task => task.task_id),
  };
}

export function mergeTaskProgressBaseline(current: TaskProgressBaseline, restored: TaskProgressBaseline): TaskProgressBaseline {
  return {
    confirmedTaskIds: Array.from(new Set([...current.confirmedTaskIds, ...restored.confirmedTaskIds])),
    excludedCompletedTaskIds: Array.from(new Set([...current.excludedCompletedTaskIds, ...restored.excludedCompletedTaskIds])),
  };
}

export function getTasksForCurrentProgress<T extends Pick<TeamTask, 'task_id'>>(tasks: T[], baseline: TaskProgressBaseline): T[] {
  const excludedTaskIds = new Set(baseline.excludedCompletedTaskIds);
  return tasks.filter(task => !excludedTaskIds.has(task.task_id));
}
