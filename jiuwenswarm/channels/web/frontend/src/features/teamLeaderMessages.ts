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

export function findActiveTeamLeaderMessage(
  messages: Message[],
  requestId?: string,
): Message | undefined {
  // 有请求标识时允许找到新用户轮之前的迟到输出；无标识事件仅属于当前用户轮。
  const latestUserIndex = requestId ? -1 : findLatestUserIndex(messages);
  for (let index = messages.length - 1; index > latestUserIndex; index -= 1) {
    const message = messages[index];
    if (
      isTeamLeaderMessage(message) &&
      message.teamStream &&
      message.teamStream.requestId === requestId
    ) {
      return message;
    }
  }
  return undefined;
}
