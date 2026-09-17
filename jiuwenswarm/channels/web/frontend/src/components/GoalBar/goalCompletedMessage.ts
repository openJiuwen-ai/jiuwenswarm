/**
 * 目标完成「回显卡片」的消息序列化。
 *
 * 目标实时跳变到 completed 时，前端本地合成一条 assistant 消息插入对话流，
 * 内容以 `goal.completed:` 前缀 + JSON 编码。MessageItem 检测到该前缀时渲染
 * GoalCompletedCard。参照 InteractionSlot/qaSummary.ts 的同一套模式。
 */

export const GOAL_COMPLETED_PREFIX = 'goal.completed:';

export interface GoalCompletedData {
  evidence?: string;
}

export function buildGoalCompletedContent(data: GoalCompletedData): string {
  return GOAL_COMPLETED_PREFIX + JSON.stringify(data);
}

export function isGoalCompletedContent(content: string | undefined | null): boolean {
  return typeof content === 'string' && content.startsWith(GOAL_COMPLETED_PREFIX);
}

export function parseGoalCompletedContent(content: string): GoalCompletedData | null {
  if (!isGoalCompletedContent(content)) return null;
  try {
    const raw = content.slice(GOAL_COMPLETED_PREFIX.length);
    const parsed = JSON.parse(raw) as GoalCompletedData;
    if (!parsed || typeof parsed !== 'object') return null;
    return parsed;
  } catch {
    return null;
  }
}
