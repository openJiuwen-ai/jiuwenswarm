import type { Message } from '../types';

function findLatestUserIndex(messages: Message[]): number {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    if (messages[index].role === 'user') {
      return index;
    }
  }
  return -1;
}

function isTeamLeaderMessage(message: Message): boolean {
  return message.id.startsWith('team-leader-');
}

/**
 * 取出 team-leader 消息的原始纯文字：已收尾的气泡内容形如 `team.leader:{"content":...}`，
 * 流式中的气泡内容则是原始文字本身。用于分段场景下按段拼接/去重。
 */
export function extractTeamLeaderRawContent(content: string | undefined): string {
  if (!content) return '';
  if (content.startsWith('team.leader:')) {
    const jsonStr = content.slice('team.leader:'.length);
    try {
      const data = JSON.parse(jsonStr);
      return typeof data?.content === 'string' ? data.content : '';
    } catch {
      return '';
    }
  }
  return content;
}

export function findActiveTeamLeaderMessage(messages: Message[]): Message | undefined {
  const latestUserIndex = findLatestUserIndex(messages);
  for (let index = messages.length - 1; index > latestUserIndex; index -= 1) {
    const message = messages[index];
    if (isTeamLeaderMessage(message) && message.isStreaming) {
      return message;
    }
  }
  return undefined;
}
