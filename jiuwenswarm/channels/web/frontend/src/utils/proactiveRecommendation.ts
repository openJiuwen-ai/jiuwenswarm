/**
 * 主动推荐卡片的消息归并工具。
 *
 * 后台 tick 触发的主 agent 话术经 WebSocket 推送，不应复用会话级
 * currentStreamId——否则在用户轮仍在 streaming 时会并入普通回答气泡，
 * 后续推荐也可能覆盖前一条推荐。与 Heartbeat 相同，按 rec_id 固定消息 ID。
 */
export function proactiveAssistantMessageId(recId: string): string {
  const clean = recId.trim();
  return clean ? `proactive-assistant-${clean}` : 'proactive-assistant-unknown';
}
