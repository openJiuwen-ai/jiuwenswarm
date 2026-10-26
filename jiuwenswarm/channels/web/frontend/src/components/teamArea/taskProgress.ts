import type { TeamTask } from '../../stores/sessionStore';

const RUNNING_PROGRESS_INITIAL = 10;
const RUNNING_PROGRESS_CAP = 85;
const RUNNING_PROGRESS_EASING_MS = 5 * 60 * 1000;

function getTaskStartTime(task: TeamTask, now: number): number {
  return typeof task.timestamp === 'number' && Number.isFinite(task.timestamp)
    ? task.timestamp
    : now;
}

/**
 * Returns a task's internal visual progress contribution basis.
 * This is intentionally not a real completion ratio: unfinished running tasks
 * can move visually with time, but only a completed status can reach 100.
 */
export function getTaskVisualProgressPercent(task: TeamTask, now = Date.now()): number {
  switch (task.status) {
    case 'completed':
      return 100;
    // Running family: the execution state plus the planning / in_review gates.
    case 'planning':
    case 'in_progress':
    case 'in_review': {
      const elapsedMs = Math.max(0, now - getTaskStartTime(task, now));
      const eased = 1 - Math.exp(-elapsedMs / RUNNING_PROGRESS_EASING_MS);
      const progress = RUNNING_PROGRESS_INITIAL
        + (RUNNING_PROGRESS_CAP - RUNNING_PROGRESS_INITIAL) * eased;
      return Math.round(Math.min(progress, RUNNING_PROGRESS_CAP));
    }
    case 'pending':
    case 'blocked':
    case 'cancelled':
      return 0;
  }
}

/**
 * Sums each task's weighted visual contribution into one 0-100 total.
 * With N tasks, each task can contribute at most 100 / N to the total.
 */
export function getTotalTaskVisualProgressPercent(tasks: TeamTask[], now = Date.now()): number {
  const actionable = tasks.filter((task) => task.status !== 'cancelled');
  if (actionable.length === 0) {
    return 0;
  }

  const totalContribution = actionable.reduce(
    (sum, task) => sum + getTaskVisualProgressPercent(task, now) / actionable.length,
    0
  );
  return Math.round(totalContribution);
}
