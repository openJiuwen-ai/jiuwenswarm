/**
 * WebSocket 事件去重键。
 *
 * 有 request_id 时用它区分「同一次操作的重复投递」和「不同操作但内容碰巧相同」
 * （bug001：两次 resume 的 goal.snapshot 内容经常完全一样）。
 *
 * chat.processing_status 同一 request_id 会连发 true / false。只认 request_id
 * 会把「忙完了」当成「开始忙」的重复扔掉；计划「执行」的补发消息就挂在 false 上。
 */

function stringifyPayloadForDedup(payload: Record<string, unknown>): string {
  try {
    const serialized = JSON.stringify(payload);
    if (!serialized) {
      return '';
    }
    return serialized.length > 800 ? serialized.slice(0, 800) : serialized;
  } catch {
    return '';
  }
}

export function makeEventDedupKey(eventName: string, payload: Record<string, unknown>): string {
  const payloadSessionId =
    typeof payload.session_id === 'string' ? payload.session_id : '';
  const payloadEventType =
    typeof payload.event_type === 'string' ? payload.event_type : '';
  const payloadRequestId =
    typeof payload.request_id === 'string' ? payload.request_id : '';
  const contentKey = payloadRequestId ? `rid:${payloadRequestId}` : stringifyPayloadForDedup(payload);
  const processingFlag =
    eventName === 'chat.processing_status'
      ? `::proc:${payload.is_processing === true ? '1' : '0'}`
      : '';
  return `${eventName}::${payloadSessionId}::${payloadEventType}::${contentKey}${processingFlag}`;
}
