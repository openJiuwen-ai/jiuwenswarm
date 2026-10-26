import type { Message } from '../../types';
import type { GitTurnDiff } from './types';

function isUserFacingTeamEvent(message: Message): boolean {
  if (message.role !== 'system' || !message.content.startsWith('team.event:')) {
    return false;
  }
  const jsonStr = message.content.slice('team.event:'.length);
  try {
    const payload = JSON.parse(jsonStr) as { event?: Record<string, unknown>; payload?: { event?: Record<string, unknown> } };
    const event = payload.event || payload.payload?.event;
    if (!event) return false;
    const type = typeof event.type === 'string' ? event.type : '';
    const fromMember = typeof event.from_member === 'string' ? event.from_member : '';
    const toMember = typeof event.to_member === 'string' ? event.to_member : '';
    const isP2PToUser = type === 'team.message.p2p' && toMember === 'user';
    const isLeaderToUser = fromMember === 'team_leader' && !type.endsWith('.p2p') && !type.endsWith('.broadcast');
    return isP2PToUser || isLeaderToUser;
  } catch {
    return false;
  }
}

function isAssistantTurnAnchor(message: Message): boolean {
  return (
    message.role === 'assistant' ||
    (
      message.role === 'system' &&
      (
        message.id.startsWith('team-leader-') ||
        message.content.startsWith('team.leader:') ||
        isUserFacingTeamEvent(message)
      )
    )
  );
}

function findDirectMessageId(messageIds: Set<string>, turn: GitTurnDiff): string | null {
  const candidates = [
    turn.assistant_message_id,
    turn.request_id,
    turn.assistant_message_id ? `team-leader-${turn.assistant_message_id}` : '',
    turn.request_id ? `team-leader-${turn.request_id}` : '',
  ];
  return candidates.find(candidate => candidate && messageIds.has(candidate)) ?? null;
}

/** Bind backend user-turn indexes to the assistant bubble rendered for that turn. */
export function bindTurnDiffsToMessages(messages: Message[], turns: GitTurnDiff[]): Map<string, GitTurnDiff[]> {
  const messageIds = new Set(messages.map(message => message.id));
  const assistantByTurnIndex = new Map<number, string>();
  const localTurnIndexByAssistantId = new Map<string, number>();
  const assistantByUserMessageId = new Map<string, string>();
  let currentTurnIndex = 0;
  let currentUserMessageId = '';

  messages.forEach(message => {
    if (message.role === 'user') {
      currentTurnIndex += 1;
      currentUserMessageId = message.id;
      return;
    }
    if (!currentTurnIndex || !isAssistantTurnAnchor(message)) return;
    // Keep the last assistant output before the next user message as the turn result.
    assistantByTurnIndex.set(currentTurnIndex, message.id);
    localTurnIndexByAssistantId.set(message.id, currentTurnIndex);
    if (currentUserMessageId) assistantByUserMessageId.set(currentUserMessageId, message.id);
  });

  // History pagination normally provides the newest contiguous message window.
  // Prefer an exact message-id anchor; otherwise align the latest local and backend turns.
  const latestBackendTurn = turns.reduce((latest, turn) => Math.max(latest, turn.turn_index), 0);
  let turnIndexOffset = Math.max(0, latestBackendTurn - currentTurnIndex);
  let latestAnchoredTurn = 0;
  turns.forEach(turn => {
    const directMessageId = findDirectMessageId(messageIds, turn) || (turn.user_message_id ? assistantByUserMessageId.get(turn.user_message_id) : undefined);
    const localTurnIndex = directMessageId ? localTurnIndexByAssistantId.get(directMessageId) : undefined;
    if (localTurnIndex && turn.turn_index >= latestAnchoredTurn) {
      latestAnchoredTurn = turn.turn_index;
      turnIndexOffset = turn.turn_index - localTurnIndex;
    }
  });

  const result = new Map<string, GitTurnDiff[]>();
  turns.forEach(turn => {
    const messageId =
      findDirectMessageId(messageIds, turn) ||
      (turn.user_message_id ? assistantByUserMessageId.get(turn.user_message_id) : undefined) ||
      assistantByTurnIndex.get(turn.turn_index - turnIndexOffset);
    if (!messageId) return;
    const boundTurns = result.get(messageId) ?? [];
    boundTurns.push(turn);
    boundTurns.sort((left, right) => left.turn_index - right.turn_index);
    result.set(messageId, boundTurns);
  });
  return result;
}
