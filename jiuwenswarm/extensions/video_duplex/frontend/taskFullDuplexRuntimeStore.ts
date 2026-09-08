import { useSyncExternalStore } from 'react';

import type { VideoLivePanelHandle } from './VideoLivePanel';

export type TaskFullDuplexRuntimeState = 'idle' | 'starting' | 'active';

interface TaskFullDuplexRuntimeSnapshot {
  state: TaskFullDuplexRuntimeState;
  error: string;
}

let controller: VideoLivePanelHandle | null = null;
let bindSession: ((sessionId: string) => void) | null = null;
let snapshot: TaskFullDuplexRuntimeSnapshot = { state: 'idle', error: '' };
const listeners = new Set<() => void>();

function publish(update: Partial<TaskFullDuplexRuntimeSnapshot>): void {
  const next = { ...snapshot, ...update };
  if (next.state === snapshot.state && next.error === snapshot.error) return;
  snapshot = next;
  listeners.forEach((listener) => listener());
}

export function registerTaskFullDuplexController(
  next: VideoLivePanelHandle | null,
  nextBindSession?: ((sessionId: string) => void) | null,
): void {
  controller = next;
  bindSession = next ? nextBindSession ?? null : null;
}

export function setTaskFullDuplexRuntimeState(state: TaskFullDuplexRuntimeState): void {
  publish({ state });
}

export function setTaskFullDuplexRuntimeError(error: string): void {
  publish({ error });
}

export async function startTaskFullDuplex(sessionId?: string): Promise<void> {
  if (!controller) {
    publish({ state: 'idle', error: '全双工运行时尚未就绪，请稍后重试。' });
    return;
  }
  if (sessionId) bindSession?.(sessionId);
  publish({ state: 'starting', error: '' });
  try {
    const started = await controller.startScreenDuplex();
    if (!started) publish({ state: 'idle' });
  } catch (error) {
    publish({
      state: 'idle',
      error: error instanceof Error ? error.message : '无法启动全双工会话。',
    });
  }
}

export function stopTaskFullDuplex(): void {
  controller?.stop();
  publish({ state: 'idle', error: '' });
}

export function useTaskFullDuplexRuntime(): TaskFullDuplexRuntimeSnapshot {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
    () => snapshot,
  );
}
