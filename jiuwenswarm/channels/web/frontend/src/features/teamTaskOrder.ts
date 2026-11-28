import { parseTimestampToMs } from '../utils/timestamp';

/** Newest task start first; fall back to update/create time only when start time is absent or invalid. */
export function sortTasksNewestFirst<T extends { timestamp?: unknown; updated_at?: unknown; created_at?: unknown }>(tasks: readonly T[]): T[] {
  const time = (task: T): number => {
    for (const value of [task.timestamp, task.updated_at, task.created_at]) {
      const parsed = parseTimestampToMs(value);
      if (Number.isFinite(parsed)) return parsed;
    }
    return 0;
  };
  return tasks.map((task, index) => ({ task, index, time: time(task) }))
    .sort((a, b) => b.time - a.time || a.index - b.index).map(item => item.task);
}
