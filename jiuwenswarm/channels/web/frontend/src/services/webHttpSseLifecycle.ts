/**
 * HTTP/SSE 任务流生命周期判定（无 i18n / fetch 依赖，便于单测）。
 */
import { isEnterprise } from '../edition';
import type { WsEvent } from '../types';

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/** STEER 注入已有 execution：正文仍走父 SSE，本请求只做短回执。 */
export function isSteerSessionInputParams(
  params?: Record<string, unknown> | null
): boolean {
  if (!params) {
    return false;
  }
  const raw = params.input_mode ?? params.runtime_mode;
  return String(raw ?? '').trim().toLowerCase() === 'steer';
}

/** STEER 短回执终态：本 SSE 只做 ACK，正文仍在父流。 */
export function isSteerReceiptSseTerminal(event: WsEvent): boolean {
  return (
    event.event === 'runtime.accepted' &&
    isRecord(event.payload) &&
    event.payload.input_boundary === 'stream'
  );
}

export function isChatSseTerminal(event: WsEvent): boolean {
  // STEER 短回执：runtime.accepted(input_boundary=stream) 后本 SSE 不再承载正文，
  // 必须结束泵流，否则会一直等 processing_status(false)（Gateway 对 STEER 不发）。
  if (isSteerReceiptSseTerminal(event)) {
    return true;
  }
  // 仅企业版使用任务级 SSE 生命周期：chat.final 只是回复段结束，
  // 必须继续读到 processing_status(false)，避免工具状态停在 pending。
  // 个人版保持原有 chat.final 即结束的协议，避免改变个人版行为。
  if (!isEnterprise()) {
    return event.event === 'chat.final' || event.event === 'chat.error';
  }
  return (
    event.event === 'chat.processing_status' &&
    isRecord(event.payload) &&
    event.payload.is_processing === false
  );
}
