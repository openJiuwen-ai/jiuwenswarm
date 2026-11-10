import { useSyncExternalStore } from 'react';
import type { VoiceStreamingState } from './streamingSession';

type Snapshot = { sessionId: string | null; state: VoiceStreamingState; error?: string; transcript: string };
let snapshot: Snapshot = { sessionId: null, state: 'idle', transcript: '' };
let controller: { start: (id: string) => Promise<void>; stop: () => void; pause: (paused: boolean) => void } | null =
  null;
const listeners = new Set<() => void>();
export function publishVoiceStreaming(update: Partial<Snapshot>): void {
  snapshot = { ...snapshot, ...update };
  listeners.forEach((listener) => listener());
}
export function registerVoiceStreaming(next: NonNullable<typeof controller>): () => void {
  controller = next;
  return () => {
    if (controller === next) controller = null;
  };
}
export function useVoiceStreamingSnapshot(): Snapshot {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
  );
}
export async function startVoiceStreaming(id: string): Promise<void> {
  if (!controller) throw new Error('disconnected');
  await controller.start(id);
}
export function stopVoiceStreaming(): void {
  controller?.stop();
}
export function pauseVoiceStreaming(paused: boolean): void {
  controller?.pause(paused);
}
