/** Fallback input for a fresh run whose first history snapshot is empty. */
import type { Message } from '../../types';
import { parseTimestampToMs } from '../../utils/timestamp';

export function cronSeedMessageId(sessionId: string): string {
  return `cron-seed-${sessionId}`;
}

/** Fill the run-now/history race without replacing persisted user input. */
export function mergeCronRunUserSeed(messages: Message[], sessionId: string, seed?: string): Message[] {
  const content = seed?.trim();
  if (!content || messages.some((message) => message.role === 'user')) return messages;
  const timestamps = messages.map((message) => parseTimestampToMs(message.timestamp)).filter(Number.isFinite);
  const timestamp = new Date((timestamps.length ? Math.min(...timestamps) : Date.now()) - 1000).toISOString();
  return [{ id: cronSeedMessageId(sessionId), role: 'user', content, timestamp }, ...messages];
}
