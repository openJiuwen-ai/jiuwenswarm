/**
 * WebSocket protocol — mirrors the gateway JSON-RPC wire format used by the
 * IDE plugin (jiuwenswarm-ide) and the web app: requests are
 * `{type:"req", id, channel_id, method, params}`, the server answers with
 * `{type:"res", id, ok, payload}` and pushes `{type:"event", event, payload}`.
 *
 * The browser channel uses channel_id "browser".
 */

import { CHANNEL_ID } from "./constants";

// ---------------------------------------------------------------------------
// Gateway wire-protocol constants
//
// Mirror the server's `ReqMethod` (outbound request names) and `EventType`
// (inbound event names) from `jiuwenswarm/common/schema/message.py`. Keeping
// them here is the single source of truth for what the extension speaks, so a
// future server-side channel adapter can reference the same names.
// ---------------------------------------------------------------------------

export const GW_METHOD = {
  CHAT_SEND: "chat.send",
  CHAT_TOOL_RESULT: "chat.tool_result",
  SESSION_LIST: "session.list",
  SESSION_CREATE: "session.create",
  SESSION_SWITCH: "session.switch",
} as const;

export const GW_EVENT = {
  CONNECTION_ACK: "connection.ack",
  CHAT_DELTA: "chat.delta",
  CHAT_FINAL: "chat.final",
  CHAT_ERROR: "chat.error",
  CHAT_TOOL_CALL: "chat.tool_call",
  PONG: "pong",
} as const;

// ---------------------------------------------------------------------------
// Outbound (browser → server)
// ---------------------------------------------------------------------------

export interface WsRequest {
  id: string;
  type: "req";
  channel_id: string;
  method: string;
  params: Record<string, unknown>;
  timestamp?: number;
}

/** Fire-and-forget chat message; streaming arrives back as events. */
export interface ChatParams {
  content: string;
  session_id: string;
  mode?: string;
  context?: string;
}

export interface ToolResultParams {
  call_id: string;
  result: unknown;
  session_id?: string;
}

export function makeRequest(
  method: string,
  params: Record<string, unknown> = {}
): WsRequest {
  return {
    id: (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2)),
    type: "req",
    channel_id: CHANNEL_ID,
    method,
    params,
    timestamp: Date.now() / 1000,
  };
}

// ---------------------------------------------------------------------------
// Inbound (server → extension UI)
//
// These are the shapes the background re-emits to the popup / side panel after
// translating the gateway's `event` frames. The UI treats them as opaque and
// keys off `type` (or `action` for background replies).
// ---------------------------------------------------------------------------

export type InboundMsgType =
  | "ack"
  | "token"
  | "done"
  | "error"
  | "sessions"
  | "session_created"
  | "tool_call"
  | "pong";

export interface InboundEnvelope {
  type: InboundMsgType;
  session_id?: string;
  payload: Record<string, unknown>;
}

export interface ConnectionAckPayload {
  session_id?: string;
  mode?: string;
  tools?: unknown[];
}

export interface TokenPayload {
  text: string;
}

export interface DonePayload {
  text: string;
}

export interface ErrorPayload {
  code: string;
  message: string;
}

export interface SessionInfo {
  session_id: string;
  title: string;
  created_at: string;
  mode: string;
}

export interface SessionsPayload {
  sessions: SessionInfo[];
}

export interface ToolCallPayload {
  tool: string;
  args: Record<string, unknown>;
  call_id: string;
}
