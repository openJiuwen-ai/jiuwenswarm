/**
 * 定时任务「立即执行」时，任务原话必须排在「正在执行中」占位上面。
 *
 * 聊天区按时间戳排序（见 buildTimelineItems），不是按数组写入顺序。
 * 占位常用服务器时间，原话若用「本机现在」，会比占位晚，排序后就掉到下面。
 * 这里不但把原话挪到列表最前，还把原话时间戳调到占位之前。
 */
import { useChatStore } from '../../stores/chatStore';
import type { Message } from '../../types';
import { parseTimestampToMs } from '../../utils/timestamp';

export function cronSeedMessageId(sessionId: string): string {
  return `cron-seed-${sessionId}`;
}

function isCronPlaceholderMessage(message: Message): boolean {
  if (message.role !== 'assistant' || typeof message.content !== 'string') {
    return false;
  }
  if (message.id.startsWith('cron-placeholder-')) {
    return true;
  }
  return (
    /正在执行中，结果稍后补发/.test(message.content)
    || /^\[cron\].*正在执行中/.test(message.content)
  );
}

function resolveSeedTimestampIso(messages: Message[]): string {
  let earliestPlaceholderMs = Number.POSITIVE_INFINITY;
  for (const message of messages) {
    if (!isCronPlaceholderMessage(message)) continue;
    const ms = parseTimestampToMs(message.timestamp);
    if (Number.isFinite(ms) && ms < earliestPlaceholderMs) {
      earliestPlaceholderMs = ms;
    }
  }
  if (Number.isFinite(earliestPlaceholderMs)) {
    return new Date(earliestPlaceholderMs - 1000).toISOString();
  }
  return new Date(Date.now() - 1000).toISOString();
}

/** 占位气泡的时间戳必须晚于任务原话，否则时间线排序会把原话甩到下面。 */
export function resolveCronPlaceholderTimestampIso(
  sessionId: string,
  eventTimestampIso: string,
): string {
  const runtime = useChatStore.getState().getRuntime(sessionId);
  const seed = runtime?.messages.find(
    (message) => message.id === cronSeedMessageId(sessionId) && message.role === 'user',
  );
  const seedMs = parseTimestampToMs(seed?.timestamp);
  const eventMs = parseTimestampToMs(eventTimestampIso);
  if (Number.isFinite(seedMs) && (!Number.isFinite(eventMs) || eventMs <= seedMs)) {
    return new Date(seedMs + 1000).toISOString();
  }
  if (Number.isFinite(eventMs)) {
    return new Date(eventMs).toISOString();
  }
  return new Date().toISOString();
}

export function ensureCronUserSeedFirst(sessionId: string, seed: string): void {
  const text = String(seed || '').trim();
  if (!sessionId || !text) return;

  const store = useChatStore.getState();
  store.ensureRuntime(sessionId);
  const runtime = store.getRuntime(sessionId);
  if (!runtime) return;

  const seedId = cronSeedMessageId(sessionId);
  const messages = runtime.messages;
  const existingIndex = messages.findIndex(
    (message) =>
      message.id === seedId
      || (message.role === 'user' && message.content === text),
  );

  const seedTimestamp = resolveSeedTimestampIso(messages);
  const seedMessage: Message =
    existingIndex >= 0
      ? {
          ...messages[existingIndex],
          id: seedId,
          content: text,
          timestamp: seedTimestamp,
        }
      : {
          id: seedId,
          role: 'user',
          content: text,
          timestamp: seedTimestamp,
        };

  const alreadyFirst =
    existingIndex === 0
    && messages[0]?.id === seedId
    && messages[0]?.content === text
    && messages[0]?.timestamp === seedTimestamp;
  if (alreadyFirst) return;

  const rest =
    existingIndex >= 0
      ? messages.filter((_, index) => index !== existingIndex)
      : messages;

  store.replaceHistoryMessages(sessionId, [seedMessage, ...rest]);
}
