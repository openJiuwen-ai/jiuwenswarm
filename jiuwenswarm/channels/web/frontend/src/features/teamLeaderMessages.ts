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
 * 队友（非 leader）落在左栏对话里的气泡。
 *
 * 队友内容原本只进集群卡片（sessionStore.teamMemberExecutionEvents），本轮起同时
 * 镜像一份到「会话 + team」的对话视图，于是按 team 切开对话时才看得出谁说了什么。
 * 前缀对齐 MessageItem 的渲染分支与 getMessageActor 的身份推断。
 */
export function isTeamMemberMessage(message: Message): boolean {
  return message.id.startsWith('team-member-');
}

/** 从 team-member-<member>@<seq> 取出成员名；取不到返回空串。 */
export function memberNameFromMessageId(id: string | undefined): string {
  if (!id || !id.startsWith('team-member-')) return '';
  const rest = id.slice('team-member-'.length);
  const at = rest.indexOf('@');
  return at < 0 ? rest : rest.slice(0, at);
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
  return findActiveStreamingMessage(messages, isTeamLeaderMessage);
}

/** 本轮里某个队友尚未收尾的气泡；切换 team 后各自找各自的。 */
export function findActiveTeamMemberMessage(
  messages: Message[],
  memberName: string
): Message | undefined {
  return findActiveStreamingMessage(
    messages,
    (message) =>
      isTeamMemberMessage(message) && memberNameFromMessageId(message.id) === memberName
  );
}

function findActiveStreamingMessage(
  messages: Message[],
  predicate: (message: Message) => boolean
): Message | undefined {
  const latestUserIndex = findLatestUserIndex(messages);
  for (let index = messages.length - 1; index > latestUserIndex; index -= 1) {
    const message = messages[index];
    if (predicate(message) && message.isStreaming) {
      return message;
    }
  }
  return undefined;
}
