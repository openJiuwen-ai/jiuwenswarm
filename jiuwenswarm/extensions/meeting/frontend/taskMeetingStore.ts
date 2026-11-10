import { useSyncExternalStore } from 'react';
import { blockTaskInput } from '../../../channels/web/frontend/src/applicationPlugins/taskInputReadiness';
import type { PlaybackState } from './meetingRuntime';

export type MeetingSnapshot = {
  state: 'idle' | 'starting' | 'active' | 'closing' | 'cleanup_failed' | 'failed';
  error?: string;
  teamExists: boolean;
  enabled: boolean;
  meetingId?: string;
  sessionId: string | null;
  playback: PlaybackState;
  speaker: string;
  voice: string;
  addressedMembers: string[];
};
type Controller = {
  start: (sessionId: string) => Promise<void>;
  stop: () => void;
  stopAudio: () => void;
};
let controller: Controller | null = null;
let snapshot: MeetingSnapshot = {
  state: 'idle',
  teamExists: false,
  enabled: false,
  sessionId: null,
  playback: 'idle',
  speaker: '',
  voice: '',
  addressedMembers: [],
};
const listeners = new Set<() => void>();
let releaseInput: (() => void) | undefined;

export function meetingErrorDetails(...messages: Array<string | undefined>): string {
  return [...new Set(messages.filter((message) => message?.trim()))].join('\n');
}

export function registerMeetingController(next: Controller): () => void {
  controller = next;
  return () => {
    if (controller === next) controller = null;
  };
}
export function publishMeeting(update: Partial<MeetingSnapshot>): void {
  snapshot = { ...snapshot, ...update };
  const previous = releaseInput;
  releaseInput = snapshot.state === 'starting' ? blockTaskInput(snapshot.sessionId) : undefined;
  previous?.();
  listeners.forEach((listener) => listener());
}
export function useMeetingSnapshot(): MeetingSnapshot {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => snapshot,
    () => snapshot,
  );
}
export async function startMeeting(sessionId: string): Promise<void> {
  if (!controller) throw new Error('disconnected');
  try {
    await controller.start(sessionId);
  } catch (error) {
    if (snapshot.sessionId === sessionId && snapshot.state === 'starting') {
      publishMeeting({ state: 'failed', error: error instanceof Error ? error.message : 'meetingStartFailed' });
    }
    throw error;
  }
}
export function stopMeeting(): void {
  controller?.stop();
}
export function stopMeetingAudio(): void {
  controller?.stopAudio();
}
