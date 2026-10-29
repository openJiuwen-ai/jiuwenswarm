import type { Message, OutputOrder } from '../types/message';
import { parseTimestampToMs } from '../utils/timestamp';

/** Server-assigned order within one output stream, also persisted in history. */
export function readOutputOrder(payload: Record<string, unknown>, key = 'output_order'): OutputOrder | undefined {
  const value = payload[key];
  if (!value || typeof value !== 'object') return undefined;
  const order = value as Record<string, unknown>;
  if (typeof order.request_id !== 'string' || !Number.isSafeInteger(order.sequence)) return undefined;
  return { requestId: order.request_id, sequence: order.sequence as number };
}

export function isSuppressedOutput(record: Record<string, unknown>): boolean {
  return record.output_suppressed === true;
}

/** Replayed output before a restored completion must not create a second stream or alter a newer one. */
export function isRestoredOutputReplay(messages: readonly Message[], payload: Record<string, unknown>): boolean {
  const marker = payload.runtime_event as Record<string, unknown> | undefined;
  if (marker?.replayed !== true || typeof payload.output_phase_id !== 'string') return false;
  const order = readOutputOrder(payload);
  const timestamp = parseTimestampToMs(payload.timestamp);
  if (!order) return false;
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (
      message.role === 'assistant' &&
      message.outputOrder?.requestId === order.requestId &&
      typeof message.outputReplayUntil === 'number' &&
      (
        order.sequence <= message.outputOrder.sequence ||
        (message.outputPhaseId === payload.output_phase_id && timestamp <= message.outputReplayUntil)
      )
    ) {
      return true;
    }
  }
  return false;
}
