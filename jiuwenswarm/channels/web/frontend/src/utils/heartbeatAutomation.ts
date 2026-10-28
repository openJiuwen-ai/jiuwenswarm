// src/utils/heartbeatAutomation.ts
/**
 * Heartbeat 自动轮的身份识别 + 独立消息归并工具。
 *
 * 后端在 Heartbeat 自动触发时把执行身份塞进 `payload.metadata.automation`（见
 * jiuwenswarm/gateway/heartbeat/scheduler.py 的 `_build_message`），结构对齐
 * `HeartbeatAutomationMetadata`。这个标记会随 user/assistant 消息落盘到 session 历史，
 * 因此实时事件链路（useWebSocket.ts 的 chat.delta/final/error、processing_status、
 * execution.error）和历史恢复链路（historyRestore.ts）必须共用同一套提取逻辑读取它——
 * 「心跳任务前端开发与接口规格说明2」§9 明确要求"不能只在实时链路增加特殊处理"。
 *
 * 归并规则（§8）：每个 run_id 建立独立消息 ID，user/assistant/error 三条按 run_id 关联，
 * 不复用全局 currentStreamId，也不按 session_id 找最后一条 assistant 消息——否则
 * Heartbeat 回答会覆盖用户上一条普通回答。
 */
import type { HeartbeatAutomationMetadata } from '../types/heartbeat';

/** 判断一个事件 payload 是否携带 Heartbeat automation 标记。 */
export function extractAutomation(payload: Record<string, unknown> | undefined | null): HeartbeatAutomationMetadata | null {
  if (!payload || typeof payload !== 'object') return null;
  // 实时事件：automation 在 payload.metadata.automation
  const meta = payload.metadata;
  if (meta && typeof meta === 'object') {
    const automation = (meta as Record<string, unknown>).automation;
    const parsed = normalizeAutomation(automation);
    if (parsed) return parsed;
  }
  // 兼容：部分事件后端可能把 automation 直接挂在 payload 顶层（历史落盘回放时常见）
  return normalizeAutomation(payload.automation);
}

function normalizeAutomation(value: unknown): HeartbeatAutomationMetadata | null {
  if (!value || typeof value !== 'object') return null;
  const obj = value as Record<string, unknown>;
  if (obj.kind !== 'heartbeat') return null;
  const jobId = typeof obj.job_id === 'string' ? obj.job_id.trim() : '';
  const runId = typeof obj.run_id === 'string' ? obj.run_id.trim() : '';
  if (!jobId || !runId) return null;
  const triggeredAt = typeof obj.triggered_at === 'number' ? obj.triggered_at : 0;
  const source = typeof obj.source === 'string' ? (obj.source as HeartbeatAutomationMetadata['source']) : 'web_rpc';
  const trigger = typeof obj.trigger === 'string' ? (obj.trigger as HeartbeatAutomationMetadata['trigger']) : 'scheduler';
  return { kind: 'heartbeat', job_id: jobId, run_id: runId, triggered_at: triggeredAt, source, trigger };
}

/** 指定 run_id 对应的 user 消息 ID（§8 归并规则）。 */
export function heartbeatUserMessageId(runId: string): string {
  return `heartbeat-user-${runId}`;
}

/** 指定 run_id 对应的 assistant 消息 ID（§8 归并规则）。 */
export function heartbeatAssistantMessageId(runId: string): string {
  return `heartbeat-assistant-${runId}`;
}

/** 指定 run_id 对应的 error 消息 ID（§8 归并规则，用于按 run_id 去重）。 */
export function heartbeatErrorMessageId(runId: string): string {
  return `heartbeat-error-${runId}`;
}

/** 判断一条已存在消息是否属于某 Heartbeat run（精确匹配三种已知消息 ID，供归并/去重使用）。 */
export function isHeartbeatRunMessage(messageId: string | undefined, runId: string): boolean {
  if (!messageId || !runId) return false;
  return (
    messageId === heartbeatUserMessageId(runId) ||
    messageId === heartbeatAssistantMessageId(runId) ||
    messageId === heartbeatErrorMessageId(runId)
  );
}
