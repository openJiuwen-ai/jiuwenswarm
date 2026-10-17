import type { OutputOrder } from '../types/message';

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
