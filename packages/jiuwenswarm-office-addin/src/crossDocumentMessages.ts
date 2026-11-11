/**
 * Messages between the conversations of open documents, as the stream shows them.
 *
 * A task for another document leaves as a ``session_send_message`` call of this
 * conversation's agent. A message from another conversation arrives as the
 * events of the turn it starts, each marked ``cross_session_agent``; while it
 * waits and runs, the mailbox reports its status to the receiving
 * conversation with ``session.message.updated``. The sender only learns that
 * its send went out.
 */
import { isRecord, type JsonObject } from './gatewayClient';

const SEND_TOOL = 'session_send_message';
export const MESSAGE_STATUS_EVENT = 'session.message.updated';
const CROSS_SESSION_ORIGIN = 'cross_session_agent';

export interface OutgoingMessage {
  callId: string;
  targetSessionId: string;
  text: string;
}

export interface IncomingMessage {
  messageId: string;
  sourceSessionId: string;
  sourceTitle: string;
  text: string;
}

function text(value: unknown): string {
  return typeof value === 'string' ? value.trim() : '';
}

function parsedArguments(value: unknown): JsonObject {
  if (isRecord(value)) return value;
  if (typeof value !== 'string' || !value.trim()) return {};
  try {
    const parsed: unknown = JSON.parse(value);
    return isRecord(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

/** A ``chat.tool_call``'s call: its id, tool name, arguments, and what the model said it is for. */
export function toolCallOf(payload: JsonObject): { callId: string; name: string; args: JsonObject; goal: string } {
  const call = isRecord(payload.tool_call) ? payload.tool_call : payload;
  return {
    callId: text(call.id) || text(call.tool_call_id) || text(payload.tool_call_id),
    name: text(call.name) || text(call.tool_name) || text(payload.tool_name),
    args: parsedArguments(call.arguments ?? call.args),
    goal: text(call.call_goal),
  };
}

/** The task a ``chat.tool_call`` sends to another conversation, or undefined. */
export function outgoingMessage(payload: JsonObject): OutgoingMessage | undefined {
  const { callId, name, args } = toolCallOf(payload);
  const targetSessionId = text(args.target_session_id);
  if (name !== SEND_TOOL || !callId || !targetSessionId) return undefined;
  return { callId, targetSessionId, text: text(args.message) };
}

/** The other conversation's message an event of the turn it started carries, or undefined. */
export function incomingMessage(payload: JsonObject): IncomingMessage | undefined {
  if (payload.message_origin !== CROSS_SESSION_ORIGIN || !isRecord(payload.cross_session)) return undefined;
  const message = payload.cross_session;
  const messageId = text(message.message_id) || text(payload.session_message_id);
  if (!messageId) return undefined;
  return {
    messageId,
    sourceSessionId: text(message.source_session_id),
    sourceTitle: text(message.source_title),
    text: typeof message.content === 'string' ? message.content : '',
  };
}

/** The message and status a ``session.message.updated`` reports, or undefined. */
export function incomingMessageStatus(payload: JsonObject): (IncomingMessage & { status: string }) | undefined {
  const message = isRecord(payload.message) ? payload.message : undefined;
  const messageId = text(message?.message_id);
  if (!message || !messageId) return undefined;
  return {
    messageId,
    sourceSessionId: text(message.source_session_id),
    sourceTitle: text(message.source_title),
    text: typeof message.content === 'string' ? message.content : '',
    status: text(message.status),
  };
}

/** The text a ``chat.tool_result`` hands back: what the model read (``rendered_result``), else the raw result. */
export function toolResultText(payload: JsonObject): string {
  const result = isRecord(payload.tool_result) ? payload.tool_result : payload;
  return text(result.rendered_result) || text(result.result) || text(result.content) || text(result.error);
}

/** Whether a ``chat.tool_result`` reports a failure. */
export function toolResultFailed(payload: JsonObject): boolean {
  const result = isRecord(payload.tool_result) ? payload.tool_result : payload;
  const status = text(result.status).toLowerCase();
  if (result.success === false || result.is_error === true) return true;
  if (['error', 'failed', 'failure', 'timeout', 'timed_out'].includes(status)) return true;
  return /^tool execution error/i.test(toolResultText(payload));
}

/** The call id a ``chat.tool_result`` answers. */
export function toolResultCallId(payload: JsonObject): string {
  const result = isRecord(payload.tool_result) ? payload.tool_result : payload;
  return text(result.tool_call_id) || text(result.id) || text(payload.tool_call_id);
}
