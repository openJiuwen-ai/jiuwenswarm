// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import type { ChatRuntime } from '../../../stores/chatStore.ts';
import type { Message } from '../../../types/message.ts';
import { extractTeamLeaderRawContent } from '../../teamLeaderMessages.ts';
import { parseTeamEventMessage } from '../../../components/ChatPanel/teamEventUtils.ts';

type PresentationReply = NonNullable<ChatRuntime['lastFinalReply']> | Message;
const teamReplies = new WeakMap<Message, {
  parts: Message[]; owner: string | null; reply: NonNullable<ChatRuntime['lastFinalReply']>;
}>();

export function presentationReplyContent(reply: PresentationReply | null): string {
  if (!reply) return '';
  if ('role' in reply && reply.role === 'system') {
    if (reply.id.startsWith('team-leader-') || reply.content.startsWith('team.leader:')) {
      return extractTeamLeaderRawContent(reply.content);
    }
    const event = parseTeamEventMessage(reply);
    return event?.type === 'team.message' && event.isLeaderToUser && (!event.toMember || event.toMember === 'user')
      ? event.content : '';
  }
  return reply.content;
}

export function lastPresentationReply(runtime: ChatRuntime | undefined): PresentationReply | null {
  if (!runtime) return null;
  const messages = runtime.messages;
  let candidate: Message | null = null;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if ((message.role === 'assistant' && !message.isStreaming || message.role === 'system') && presentationReplyContent(message)
      && !message.automation && !message.crossSession) {
      candidate = message; break;
    }
  }
  const saved = runtime.lastFinalReply;
  if (candidate?.role === 'system' && (!saved || replyInputIndex(runtime, saved) <= replyInputIndex(runtime, candidate))) {
    // A final reply recorded for the same turn belongs to an assistant answer:
    // owner-mirrored Team events share that turn and must not shadow it.
    if (saved && saved.content && replyInputIndex(runtime, saved) === replyInputIndex(runtime, candidate)) return saved;
    const projected = projectTeamReply(runtime, candidate);
    if (projected.content) return projected;
    return saved && replyInputIndex(runtime, saved) >= replyInputIndex(runtime, candidate) ? saved : candidate;
  }
  return saved && replyInputIndex(runtime, saved) >= replyInputIndex(runtime, candidate) ? saved : candidate;
}

function projectTeamReply(runtime: ChatRuntime, last: Message): PresentationReply {
  const ownerIndex = replyInputIndex(runtime, last), owner = runtime.messages[ownerIndex];
  const requestId = owner?.inputRequestId || owner?.outputOrder?.requestId || last.outputOrder?.requestId || last.teamStream?.requestId;
  let currentUser = -1;
  const parts: Message[] = [];
  for (let index = 0; index < runtime.messages.length; index += 1) {
    const message = runtime.messages[index];
    if (message.role === 'user') currentUser = index;
    const partRequest = message.outputOrder?.requestId || message.teamStream?.requestId;
    const belongs = ownerIndex < 0 ? message === last : requestId && partRequest ? requestId === partRequest : currentUser === ownerIndex;
    if (belongs && message.role === 'system' && presentationReplyContent(message)
      && !message.automation && !message.crossSession) parts.push(message);
    if (message === last) break;
  }
  const ownerId = owner?.id ?? null, cached = teamReplies.get(last);
  if (cached && cached.owner === ownerId && cached.parts.length === parts.length
    && parts.every((part, index) => cached.parts[index] === part)) return cached.reply;
  const reply = { content: parts.map(presentationReplyContent).join('\n\n'), userMessageId: ownerId, requestId };
  teamReplies.set(last, { parts, owner: ownerId, reply });
  return reply;
}

function replyInputIndex(runtime: ChatRuntime, reply: PresentationReply | null): number {
  if (!reply) return -1;
  const messages = runtime.messages;
  if ('userMessageId' in reply) {
    return messages.findIndex((message) => message.role === 'user' && message.id === reply.userMessageId);
  }
  const requestId = reply.outputOrder?.requestId || reply.teamStream?.requestId;
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
      const fullContent = role === 'user' ? reply.content : presentationReplyContent(reply);
      if (offset > fullContent.length) throw new Error('REPLY_CHANGED');
      const content = fullContent.slice(offset, offset + 16 * 1024);
      const end = offset + content.length;
      return { content, nextOffset: end < fullContent.length ? end : null };
    },
  };
}
