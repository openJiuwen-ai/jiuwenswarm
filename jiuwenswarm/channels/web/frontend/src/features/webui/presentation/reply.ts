import type { ChatRuntime } from '../../../stores/chatStore.ts';
import type { Message } from '../../../types/message.ts';

type PresentationReply = NonNullable<ChatRuntime['lastFinalReply']> | Message;

export function lastPresentationReply(runtime: ChatRuntime | undefined): PresentationReply | null {
  if (!runtime) return null;
  const messages = runtime.messages;
  let candidate: Message | null = null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === 'assistant' && !message.isStreaming && message.content
      && !message.automation && !message.crossSession && !message.isProactiveRecommendation) {
      candidate = message; break;
    }
  }
  const saved = runtime.lastFinalReply;
  return saved && replyInputIndex(runtime, saved) >= replyInputIndex(runtime, candidate) ? saved : candidate;
}

function replyInputIndex(runtime: ChatRuntime, reply: PresentationReply | null): number {
  if (!reply) return -1;
  const messages = runtime.messages;
  if ('userMessageId' in reply) {
    return messages.findIndex((message) => message.role === 'user' && message.id === reply.userMessageId);
  }
  const requestId = reply.outputOrder?.requestId;
  if (requestId) {
    const owner = messages.findIndex((message) => message.role === 'user'
      && (message.inputRequestId === requestId || message.outputOrder?.requestId === requestId));
    if (owner >= 0) return owner;
  }
  for (let index = messages.indexOf(reply) - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'user') {
      return requestId && messages[index].inputRequestId !== undefined ? -1 : index;
    }
  }
  return -1;
}

export function presentationReplyUserMessage(runtime: ChatRuntime | undefined, reply: PresentationReply | null): Message | null {
  if (!runtime) return null;
  return runtime.messages[replyInputIndex(runtime, reply)] ?? null;
}

export function lastPresentationUserMessage(runtime: ChatRuntime | undefined): Message | null {
  const messages = runtime?.messages ?? [];
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'user') return messages[index];
  }
  return null;
}

/** Only the latest public user message and reply are readable; identity guards concurrent replacement, without a history cache. */
export function createPresentationReplyReader() {
  const ids = new WeakMap<object, number>();
  let sequence = 0;
  function identify(reply: object | null): number | null {
    if (!reply) return null;
    let id = ids.get(reply);
    if (!id) { id = ++sequence; ids.set(reply, id); }
    return id;
  }
  return {
    identify,
    read(runtime: ChatRuntime | undefined, replyId: number, offset: number, role: 'user' | 'assistant' = 'assistant') {
      const reply = role === 'user' ? lastPresentationUserMessage(runtime) : lastPresentationReply(runtime);
      if (!reply || identify(reply) !== replyId) throw new Error('REPLY_CHANGED');
      if (offset > reply.content.length) throw new Error('REPLY_CHANGED');
      const content = reply.content.slice(offset, offset + 16 * 1024);
      const end = offset + content.length;
      return { content, nextOffset: end < reply.content.length ? end : null };
    },
  };
}
