export type PendingInstallMode = 'catalog' | 'group-picker';

export interface PendingInstallJob {
  id: string;
  mode: PendingInstallMode;
  pendingConnectors: string[];
}

export interface PendingInstallQueue {
  active: PendingInstallJob | null;
  waiting: PendingInstallJob[];
}

export function createPendingInstallQueue(): PendingInstallQueue {
  return { active: null, waiting: [] };
}

export function enqueuePendingInstall(queue: PendingInstallQueue, job: PendingInstallJob): PendingInstallQueue {
  if (queue.active?.id === job.id || queue.waiting.some((item) => item.id === job.id)) return queue;
  if (!queue.active) return { active: job, waiting: queue.waiting };
  return { ...queue, waiting: [...queue.waiting, job] };
}

export function advancePendingInstallQueue(queue: PendingInstallQueue): {
  finished: PendingInstallJob | null;
  queue: PendingInstallQueue;
} {
  const [next, ...waiting] = queue.waiting;
  return {
    finished: queue.active,
    queue: { active: next ?? null, waiting },
  };
}
