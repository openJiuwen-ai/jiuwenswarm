import type { Message } from '../types';

interface PrepareUserTurnOptions {
  bumpUserInputVersion: () => void;
  stopAudio: () => void;
  clearContextWindowUsage: () => void;
  addMessage: (message: Message) => void;
  now?: () => number;
  timestamp?: () => string;
}

export function prepareUserTurn(
  content: string,
  {
    bumpUserInputVersion,
    stopAudio,
    clearContextWindowUsage,
    addMessage,
    now = Date.now,
    timestamp = () => new Date().toISOString(),
  }: PrepareUserTurnOptions,
): void {
  bumpUserInputVersion();
  stopAudio();
  clearContextWindowUsage();
  addMessage({
    id: `user-${now()}`,
    role: 'user',
    content,
    timestamp: timestamp(),
  });
}
