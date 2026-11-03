import { Message } from '../../types';
import { parseTeamHandoff, type TeamHandoff } from '../teamArea/groupChatTimeline';

export interface ParsedTeamEvent {
  type: string;
  messageId: string;
  fromMember: string;
  toMember?: string;
  content: string;
  timestamp?: number;
  isP2P: boolean;
  isBroadcast: boolean;
  isLeaderToUser: boolean;
  /** 框架模板消息（调度器交接等）的展示描述；普通消息为 undefined。 */
  handoff?: TeamHandoff;
}

export function formatTeamEventTime(ts: number | undefined): string {
  if (!ts) return '';
  const date = new Date(ts);
  return date.toLocaleTimeString('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

export function parseTeamEventMessage(message: Message): ParsedTeamEvent | null {
  const { content } = message;
  if (!content?.startsWith('team.event:')) {
    return null;
  }

  const jsonStr = content.slice('team.event:'.length);
  try {
    const payload = JSON.parse(jsonStr);
    const event = payload.event || payload.payload?.event;
    if (!event) {
      return null;
    }

    const type = typeof event.type === 'string' ? event.type : '';
    const messageId = typeof event.message_id === 'string' ? event.message_id : '';
    const fromMember = typeof event.from_member === 'string' ? event.from_member : '';
    const toMember = typeof event.to_member === 'string' ? event.to_member : undefined;
    const messageContent = typeof event.content === 'string' ? event.content : '';
    const timestamp = typeof event.timestamp === 'number' ? event.timestamp : undefined;
    const isP2P = type.endsWith('.p2p');
    const isBroadcast = type.endsWith('.broadcast');

    return {
      type,
      messageId,
      fromMember,
      toMember,
      content: messageContent,
      timestamp,
      isP2P,
      isBroadcast,
      isLeaderToUser: fromMember === 'team_leader' && !isP2P && !isBroadcast,
      handoff: parseTeamHandoff(event.handoff),
    };
  } catch {
    return null;
  }
}

export function isTeamMemberCollaborationMessage(message: Message): boolean {
  const event = parseTeamEventMessage(message);
  if (!event) {
    return false;
  }
  return !event.isLeaderToUser && !isTeamP2PMessageToUser(event);
}

/** Only explicit user-facing Team events are mirrored; collaboration stays Team-local. */
export function toOwnerTeamMessage(message: Message): Message | null {
  if (!message.teamId || message.role !== 'system') return null;
  const event = parseTeamEventMessage(message);
  if (!event || (!event.isLeaderToUser && !isTeamP2PMessageToUser(event))) return null;
  return {
    ...message,
    id: `owner-team-message-${message.teamId}-${event.messageId || message.id}`,
    teamId: undefined,
    sourceTeamId: message.teamId,
  };
}

/** Merge restored and live messages by logical Team event identity. */
export function mergeUniqueMessages(messages: Message[]): Message[] {
  const seen = new Set<string>();
  return messages.filter((message) => {
    const event = parseTeamEventMessage(message);
    const key = event
      ? ['team', message.sourceTeamId || '', event.type, event.messageId, event.fromMember, event.toMember || '', event.timestamp || '', event.content].join(':')
      : `${message.id}:${message.content}`;
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function isTeamActivityMessage(message: Message): boolean {
  return Boolean(parseTeamEventMessage(message));
}

export function isTeamP2PMessageToUser(event: ParsedTeamEvent): boolean {
  return event.type === 'team.message.p2p' && event.toMember === 'user';
}
