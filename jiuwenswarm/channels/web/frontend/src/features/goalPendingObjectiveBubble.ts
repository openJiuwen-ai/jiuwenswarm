/**
 * Goal 用户气泡延迟落地（效果 B）。
 *
 * 智能体忙碌时设目标：command.goal 立刻发出（GoalBar「已设置」），
 * 但用户气泡不立刻出现——等上一轮空 chat.final / processing 结束再入列，
 * 与后端推迟写入 Goal 用户历史对齐，避免插进当前回答中间拆轮。
 */

import { useChatStore } from '../stores/chatStore';

/** 把暂存的目标用户气泡落到时间线末尾；没有暂存则 noop。 */
export function flushPendingGoalObjectiveBubble(sessionId: string): void {
  useChatStore.getState().flushPendingGoalObjectiveBubble(sessionId);
}

/**
 * 空闲时立刻落气泡；忙碌时只暂存（界面暂不显示），等 flush。
 * 无论哪种都应由调用方继续 onSetGoal / command.goal。
 */
export function queueOrAddGoalObjectiveMessage(sessionId: string, content: string): void {
  const trimmed = content.trim();
  if (!trimmed) {
    return;
  }
  useChatStore.getState().queueOrAddGoalObjectiveMessage(sessionId, trimmed);
}
