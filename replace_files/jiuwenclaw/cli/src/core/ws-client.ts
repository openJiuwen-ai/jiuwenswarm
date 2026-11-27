import WebSocket from "ws";
import type { Frame, ResFrame } from "./protocol.js";
import { isResFrame } from "./protocol.js";

export type FrameHandler = (frame: Frame) => void;
export type ConnectionStatus = "idle" | "connecting" | "connected" | "reconnecting" | "auth_failed";

interface PendingRequest {
  resolve: (frame: ResFrame) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class WsClient {
  private ws: WebSocket | null = null;
  private readonly url: string;
  private readonly token: string;
  private handlers: FrameHandler[] = [];
  private pending = new Map<string, PendingRequest>();
  private retryCount = 0;
  private readonly maxRetries = 5;
  private readonly baseDelay = 1000;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private _status: ConnectionStatus = "idle";
  private statusListeners: Array<(status: ConnectionStatus) => void> = [];
  private closeCode = 0;
  private closeReason = "";

  constructor(url: string, token = "") {
    this.url = url;
    this.token = token;
  }

  get status(): ConnectionStatus {
    return this._status;
  }

  get lastCloseCode(): number {
    return this.closeCode;
  }

  get lastCloseReason(): string {
    return this.closeReason;
  }

  onStatusChange(fn: (status: ConnectionStatus) => void): () => void {
    this.statusListeners.push(fn);
    return () => {
      this.statusListeners = this.statusListeners.filter((item) => item !== fn);
    };
  }

  onFrame(handler: FrameHandler): () => void {
    this.handlers.push(handler);
    return () => {
      this.handlers = this.handlers.filter((item) => item !== handler);
    };
  }

  connect(): void {
    this.setStatus("connecting");
    this.doConnect();
  }

  disconnect(): void {
    this.clearReconnectTimer();
    if (this.ws) {
      const ws = this.ws;
      this.ws = null;
      ws.removeAllListeners();
      if (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING) {
        ws.close();
      }
    }
    this.rejectAllPending(new Error("disconnected"));
    this.setStatus("idle");
  }

  send(frame: Frame): void {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(frame));
    }
  }

  request(
    id: string,
    method: string,
    params: Record<string, unknown>,
    timeoutMs = 30000,
  ): Promise<ResFrame> {
    return new Promise((resolve, reject) => {
      if (this.ws?.readyState !== WebSocket.OPEN) {
        reject(new Error(`socket not connected: ${method}`));
        return;
      }
      const frame = { type: "req" as const, id, method, params };
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`request timeout: ${method}`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.send(frame);
    });
  }

  private setStatus(status: ConnectionStatus): void {
    this._status = status;
    for (const listener of this.statusListeners) {
      listener(status);
    }
  }

  private doConnect(): void {
    const headers: Record<string, string> = {};
    if (this.token) {
      headers.Authorization = `Bearer ${this.token}`;
    }

    this.ws = new WebSocket(this.url, { headers });

    this.ws.on("open", () => {
      this.retryCount = 0;
      this.setStatus("connected");
    });

    this.ws.on("message", (data: WebSocket.Data) => {
      try {
        const frame = JSON.parse(data.toString()) as Frame;
        this.dispatchFrame(frame);
      } catch {
        // Ignore malformed frames.
      }
    });

    this.ws.on("close", (code: number, reason: Buffer) => {
      this.closeCode = code;
      this.closeReason = reason.toString();
      this.ws = null;

      if (code === 1008) {
        this.setStatus("auth_failed");
        this.rejectAllPending(new Error(`auth failed: ${this.closeReason}`));
        return;
      }

      if (code === 1013) {
        this.setStatus("idle");
        this.rejectAllPending(new Error("cli channel busy"));
        return;
      }

      this.handleReconnect();
    });

    this.ws.on("error", () => {
      // The close handler runs afterwards.
    });
  }

  private dispatchFrame(frame: Frame): void {
    if (isResFrame(frame)) {
      const pending = this.pending.get(frame.id);
      if (pending) {
        clearTimeout(pending.timer);
        this.pending.delete(frame.id);
        if (frame.ok) {
          pending.resolve(frame);
        } else {
          pending.reject(new Error(frame.error ?? `request failed: ${frame.code ?? "unknown"}`));
        }
        return;
      }
    }

    for (const handler of this.handlers) {
      try {
        handler(frame);
      } catch {
        // Ignore handler errors.
      }
    }
  }

  private handleReconnect(): void {
    if (this.retryCount >= this.maxRetries) {
      this.setStatus("idle");
      return;
    }

    this.setStatus("reconnecting");
    const delay = Math.min(this.baseDelay * 2 ** this.retryCount, 30000);
    this.retryCount += 1;
    this.reconnectTimer = setTimeout(() => this.doConnect(), delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private rejectAllPending(error: Error): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(error);
    }
    this.pending.clear();
  }
}
