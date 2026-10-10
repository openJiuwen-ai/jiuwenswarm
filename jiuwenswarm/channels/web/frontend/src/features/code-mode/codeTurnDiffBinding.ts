import type { Message } from '../../types';
import type { GitTurnDiff } from './types';
import { latestModifiedTurn } from './turnChangeState.js';

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
  // Cron persists its request as ``cron-<run_id>`` but the WebSocket timeline
  // renders its final output as ``cron-final-<run_id>``. Treat both as the
  // same turn anchor so the latest cron change card retains undo/redo actions.
  const cronFinalMessageId = turn.request_id.startsWith('cron-')
    ? `cron-final-${turn.request_id.slice('cron-'.length)}`
    : '';
  const candidates = [
    turn.assistant_message_id,
    turn.request_id,
    cronFinalMessageId,
    turn.assistant_message_id ? `team-leader-${turn.assistant_message_id}` : '',
    turn.request_id ? `team-leader-${turn.request_id}` : '',
  ];
  return candidates.find(candidate => candidate && messageIds.has(candidate)) ?? null;
}

/**
 * Live message timestamps use the browser clock while the turn timestamp is
 * server-generated. With a fast browser clock the turn's own user message
 * lands just after the turn start, and a strict "at or before" search would
 * fall through to the previous chat round (or drop the card on the first
 * turn). Keep the closest message at or before the turn start, and only fall
 * back to the closest message inside the skew budget when that candidate is
 * itself implausibly far — later chat rounds and off-screen windows start
 * beyond the budget, so they never borrow the card.
 */
const LIVE_CLOCK_SKEW_MS = 2 * 60_000;

function locateUserMessageByTurnTime(messages: Message[], turnTime: number): number {
  if (!Number.isFinite(turnTime)) return -1;
  let beforeIndex = -1;
  let beforeGap = Number.POSITIVE_INFINITY;
  let afterIndex = -1;
  let afterGap = Number.POSITIVE_INFINITY;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role !== 'user') continue;
    const at = Date.parse(message.timestamp);
    if (!Number.isFinite(at)) continue;
    if (at <= turnTime) {
      const gap = turnTime - at;
      if (gap < beforeGap) {
        beforeGap = gap;
        beforeIndex = index;
      }
    } else if (at - turnTime <= LIVE_CLOCK_SKEW_MS) {
      const gap = at - turnTime;
      if (gap <= afterGap) {
        afterGap = gap;
        afterIndex = index;
      }
    }
  }
  if (afterIndex >= 0 && (beforeIndex < 0 || beforeGap > LIVE_CLOCK_SKEW_MS)) {
    return afterIndex;
  }
  return beforeIndex;
}

/** Bind only the latest file-editing turn; never infer a paginated turn offset. */
export function bindTurnDiffsToMessages(messages: Message[], turns: GitTurnDiff[]): Map<string, GitTurnDiff[]> {
  const turn = latestModifiedTurn(turns);
  const result = new Map<string, GitTurnDiff[]>();
  if (!turn) return result;

  const directId = findDirectMessageId(new Set(messages.map((message) => message.id)), turn);
  if (directId) return new Map([[directId, [turn]]]);

  let userIndex = messages.findIndex((message) => message.role === 'user' && message.id === turn.user_message_id);
  if (userIndex < 0) {
    // Live user ids are temporary. Locate the user interval containing the
    // persisted turn start, instead of moving the card as more chats arrive.
    userIndex = locateUserMessageByTurnTime(messages, Date.parse(turn.timestamp));
  }
  if (userIndex < 0) return result;

  let messageId: string | null = null;
  for (let index = userIndex + 1; index < messages.length; index += 1) {
    const message = messages[index];
    if (message.role === 'user') break;
    if (isAssistantTurnAnchor(message)) messageId = message.id;
  }
  if (messageId) result.set(messageId, [turn]);
  return result;
}

/**
 * 为每个已绑定的轮次选出唯一的卡片锚点消息。
 *
 * 后端把同一 request 的所有记录都写成同一个 id（`<request_id>:assistant`），
 * 而每个 `chat.final` 在历史恢复时都会变成一条独立消息，因此
 * ``bindTurnDiffsToMessages`` 返回的 id 键可能命中**多条**消息；渲染层是
 * 「每条消息问一次要不要出卡片」（`MessageList` 对每个 message 项调一次
 * `renderAfterMessage`），直接按 id 命中就会把同一个轮次的卡片重复渲染成
 * 多张（该轮有几条 `chat.final` 就有几张）。
 *
 * 这里每个 id 只保留**最后一条**消息作为锚点——同一 id 必属同一 request，故
 * 最后一条即该轮末尾，卡片位置语义也正确。渲染层仅在该锚点上出卡片。
 *
 * 用消息对象身份（而非 id）区分：`messages` 从 store 到 `MessageList` 全程
 * 透传同一批对象引用，无需依赖 `renderKey` 是否已赋值。
 */
export function resolveTurnCardAnchors(
  messages: Message[],
  turnsByMessageId: Map<string, GitTurnDiff[]>
): Set<Message> {
  const anchors = new Set<Message>();
  if (turnsByMessageId.size === 0) return anchors;

  const anchorByMessageId = new Map<string, Message>();
  for (const message of messages) {
    if (turnsByMessageId.has(message.id)) {
      anchorByMessageId.set(message.id, message);
    }
  }
  for (const anchor of anchorByMessageId.values()) {
    anchors.add(anchor);
  }
  return anchors;
}
