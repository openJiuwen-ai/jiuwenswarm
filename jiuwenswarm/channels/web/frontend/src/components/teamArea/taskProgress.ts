import type { TeamTask } from '../../stores/sessionStore';

/**
 * Returns the board's 0-100 progress from real task counts:
 * completed / (all tasks - cancelled). Cancelled tasks ("任务失败") never
 * count toward the denominator, and the value only changes when a task's
 * status changes.
 */
export function getTaskProgressPercent(tasks: Pick<TeamTask, 'status'>[]): number {
  const actionable = tasks.filter((task) => task.status !== 'cancelled');
  if (actionable.length === 0) {
    return 0;
  }
  const completed = actionable.filter((task) => task.status === 'completed').length;
  return Math.round((completed / actionable.length) * 100);
}
