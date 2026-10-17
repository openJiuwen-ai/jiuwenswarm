/**
 * WebSocket client for the JiuwenSwarm background service worker.
 *
 * Speaks the gateway JSON-RPC protocol (same as jiuwenswarm-ide): outbound
 * `{type:"req", id, channel_id, method, params}`, inbound `res` frames resolve
 * pending requests and `event` frames are translated into InboundEnvelope
 * objects and dispatched to registered handlers.
 *
 * Reconnects automatically with exponential back-off on unexpected close.
 */

import { createLogger } from "@shared/logger";
import { InboundEnvelope, makeRequest, GW_EVENT } from "@shared/protocol";
import { loadSettings } from "@shared/storage";
import { WS_URL } from "@shared/constants";

export type EventHandler = (envelope: InboundEnvelope) => void;
export type StatusChangeHandler = (connected: boolean) => void;

const log = createLogger("bg/ws");

const BACKOFF_MS = [1_000, 2_000, 5_000, 10_000, 30_000];

type PendingRequest = {
  resolve: (payload: Record<string, unknown>) => void;
  reject: (err: Error) => void;
  timer: number;
};

const REQUEST_TIMEOUT_MS = 15_000;

export class WsClient {
  private _ws: WebSocket | null = null;
  private _handlers: Set<EventHandler> = new Set();
  private _pending = new Map<string, PendingRequest>();
  private _retryCount = 0;
  private _intentionalClose = false;
  private _onStatusChange: StatusChangeHandler | null = null;

  /** Register a callback fired whenever the connection state flips. */
  onStatusChange(handler: StatusChangeHandler): void {
    this._onStatusChange = handler;
  }

  get isConnected(): boolean {
    return this._ws?.readyState === WebSocket.OPEN;
  }

  async connect(): Promise<void> {
    if (this.isConnected) return;
    const settings = await loadSettings();
    const url = WS_URL(settings.host, settings.port);
    this._intentionalClose = false;
    this._open(url);
  }

  disconnect(): void {
    this._intentionalClose = true;
    this._ws?.close();
    this._ws = null;
  }

  /**
   * Send a request and await its `res` frame.
   */
  request(method: string, params: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
      if (!this.isConnected || !this._ws) {
        reject(new Error("WebSocket not connected"));
        return;
      }
      const id = this._nextId();
      const timer = setTimeout(() => {
        this._pending.delete(id);
        reject(new Error(`Request '${method}' timed out`));
      }, REQUEST_TIMEOUT_MS);
      this._pending.set(id, { resolve, reject, timer });
      const req = makeRequest(method, params);
      req.id = id;
      this._ws.send(JSON.stringify(req));
    });
  }

  /**
   * Fire-and-forget request (no `res` is expected) — used for chat.send.
   * Returns the generated request id so the caller can correlate streaming
   * events if needed.
   */
  send(method: string, params: Record<string, unknown> = {}): string | null {
    if (!this.isConnected || !this._ws) {
      log.warn("send() called while disconnected — dropping", method);
      return null;
    }
    const id = this._nextId();
    const req = makeRequest(method, params);
    req.id = id;
    this._ws.send(JSON.stringify(req));
    return id;
  }

  onEvent(handler: EventHandler): () => void {
    this._handlers.add(handler);
    return () => this._handlers.delete(handler);
  }

  // -------------------------------------------------------------------------
  // Internals
  // -------------------------------------------------------------------------

  private _nextId(): string {
    return (crypto.randomUUID ? crypto.randomUUID() : String(Date.now()) + Math.random().toString(16).slice(2));
  }

  private _open(url: string): void {
    log.info("connecting to", url);
    const ws = new WebSocket(url);

    ws.onopen = () => {
      log.info("connected");
      this._retryCount = 0;
      this._ws = ws;
      this._onStatusChange?.(true);
    };

    ws.onmessage = (ev: MessageEvent) => {
      this._handleMessage(ev.data as string);
    };

    ws.onerror = (ev) => {
      log.warn("websocket error", ev);
    };

    ws.onclose = () => {
      this._ws = null;
      this._onStatusChange?.(false);
      // Fail any in-flight requests
      for (const { reject, timer } of this._pending.values()) {
        clearTimeout(timer);
        reject(new Error("WebSocket closed"));
      }
      this._pending.clear();
      if (this._intentionalClose) {
        log.info("disconnected (intentional)");
        return;
      }
      const delay = BACKOFF_MS[Math.min(this._retryCount, BACKOFF_MS.length - 1)];
      log.info(`reconnecting in ${delay}ms (attempt ${this._retryCount + 1})`);
      this._retryCount++;
      setTimeout(() => this._open(url), delay);
    };
  }

  private _handleMessage(raw: string): void {
    let msg: Record<string, unknown>;
    try {
      msg = JSON.parse(raw) as Record<string, unknown>;
    } catch {
      log.warn("could not parse inbound message");
      return;
    }

    if (msg.type === "res") {
      const id = msg.id as string;
      const pending = id ? this._pending.get(id) : undefined;
      if (!pending) return;
      clearTimeout(pending.timer);
      this._pending.delete(id);
      if (msg.ok) {
        pending.resolve((msg.payload as Record<string, unknown>) || {});
      } else {
        const err = ((msg.payload as Record<string, unknown>)?.error as string) || (msg.error as string) || "Request failed";
        pending.reject(new Error(err));
      }
      return;
    }

    if (msg.type === "event") {
      const eventName = msg.event as string;
      const payload = (msg.payload as Record<string, unknown>) || {};
      const sessionId = (payload.session_id as string) || (msg.session_id as string) || undefined;
      const envelope = this._translateEvent(eventName, payload, sessionId);
      if (envelope) {
        for (const h of this._handlers) {
          try {
            h(envelope);
          } catch (e) {
            log.error("handler threw", e);
          }
        }
      }
    }
  }

  private _translateEvent(
    name: string,
    payload: Record<string, unknown>,
    sessionId?: string
  ): InboundEnvelope | null {
    const withSession = (type: InboundEnvelope["type"], p: Record<string, unknown>): InboundEnvelope =>
      sessionId ? { type, session_id: sessionId, payload: p } : { type, payload: p };

    switch (name) {
      case GW_EVENT.CONNECTION_ACK:
        return withSession("ack", payload);
      case GW_EVENT.CHAT_DELTA:
        return withSession("token", { text: payload.text ?? payload.content ?? "" });
      case GW_EVENT.CHAT_FINAL:
        return withSession("done", { text: payload.content ?? payload.text ?? "" });
      case GW_EVENT.CHAT_ERROR:
        return withSession("error", {
          message: (payload.message as string) || (payload.error as string) || "Unknown error",
          code: (payload.code as string) || "",
        });
      case GW_EVENT.CHAT_TOOL_CALL: {
        const tc = (payload.tool_call as Record<string, unknown>) || {};
        return withSession("tool_call", {
          tool: (tc.name as string) || "",
          args: (tc.arguments as Record<string, unknown>) || {},
          call_id: (tc.id as string) || (payload.request_id as string) || "",
        });
      }
      case GW_EVENT.PONG:
        return withSession("pong", payload);
      default:
        // Other events (chat.reasoning, chat.tool_update, history.*, team.* …)
        // are not consumed by the extension UI yet.
        return null;
    }
  }
}
