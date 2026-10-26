import { Message } from '../../types';

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

export function isTeamActivityMessage(message: Message): boolean {
  return Boolean(parseTeamEventMessage(message));
}

export function isTeamP2PMessageToUser(event: ParsedTeamEvent): boolean {
  return event.type === 'team.message.p2p' && event.toMember === 'user';
}
