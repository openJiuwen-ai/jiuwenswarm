import type { ApplicationTaskProgress } from './taskProgressStore';

export interface TaskTimelineClock {
  active: boolean;
  endMs?: number;
}

/** Resolve task clocks using the same timestamp boundaries as the chat timeline. */
export function taskTimelineClock(
  tasks: ApplicationTaskProgress[],
  startMs: number,
  nextStartMs: number,
  toolCallIds: string[],
): TaskTimelineClock | undefined {
  const matched = tasks.filter(task => task.pluginId === 'video-duplex' && (
    toolCallIds.some(id => id.startsWith(`full-duplex-core-${task.id}-`))
    || (task.createdAt >= startMs && task.createdAt < nextStartMs)
  ));
  if (!matched.length) return undefined;
  return {
    active: matched.some(task => task.status === 'running' || task.status === 'cancelling'),
    endMs: Math.max(...matched.map(task => task.finishedAt || 0)) || undefined,
  };
}
