import { isGoalCompletedContent } from '../components/GoalBar/goalCompletedMessage';
import type { Message } from '../types';

/** 判断消息能否承载 chat.file 卡片时用到的最小字段。 */
type MessageFileHost = Pick<Message, 'role' | 'id' | 'content'>;

/**
 * chat.file 只能挂到「气泡本身会渲染 fileItems」的消息上：
 * - assistant 正文（含流式消息、qa.summary 回显卡）；
 * - 团队模式 leader 气泡（role=system 且 id 以 team-leader- 开头）。
 *
 * goal 完成卡只渲染自己的卡片，挂上 fileItems 会连文件一起藏掉。
 * 实时 `addFileItems` 与历史恢复共用这道判断；挂不上时调用方退回独立文件消息。
 */
export function canMessageHostFileItems(message: MessageFileHost | null | undefined): boolean {
  if (!message) {
    return false;
  }
  const assistantLike =
    message.role === 'assistant' ||
    (message.role === 'system' && Boolean(message.id?.startsWith('team-leader-')));
  if (!assistantLike) {
    return false;
  }
  return !isGoalCompletedContent(message.content);
}
