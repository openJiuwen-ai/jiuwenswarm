import WebSocket from 'ws';
import { JiuwenMessage } from './protocol';

export type WsStatus = 'disconnected' | 'connecting' | 'connected' | 'reconnecting';

export type WsEventMap = {
  status: (s: WsStatus) => void;
  message: (m: JiuwenMessage) => void;
  error: (e: Error) => void;
};

const BACKOFF_MS = [1000, 2000, 4000, 8000, 16000, 30000];
const DEFAULT_PING_INTERVAL_MS = 30_000;

export class WsClient {
  private ws: WebSocket | null = null;
  private status: WsStatus = 'disconnected';
  private retryCount = 0;
  private retryTimer: NodeJS.Timeout | null = null;
  private pingTimer: NodeJS.Timeout | null = null;
  private destroyed = false;
  private listeners: Partial<{ [K in keyof WsEventMap]: WsEventMap[K][] }> = {};
  // On macOS, Node.js 17+ resolves "localhost" to ::1 (IPv6) before 127.0.0.1 (IPv4).
  // If the server only listens on IPv4 the connection fails immediately. Force IPv4.
  private readonly url: string;

  constructor(
    url: string,
    private pingIntervalMs: number = DEFAULT_PING_INTERVAL_MS,
  ) {
    this.url = url.replace('://localhost:', '://127.0.0.1:');
  }

  on<K extends keyof WsEventMap>(event: K, listener: WsEventMap[K]): this {
    if (!this.listeners[event]) this.listeners[event] = [] as any;
    (this.listeners[event] as any[]).push(listener);
    return this;
  }

  off<K extends keyof WsEventMap>(event: K, listener: WsEventMap[K]): this {
    const arr = this.listeners[event] as any[] | undefined;
    if (arr) {
      const idx = arr.indexOf(listener);
      if (idx >= 0) arr.splice(idx, 1);
    }
    return this;
  }

  private emit<K extends keyof WsEventMap>(event: K, ...args: Parameters<WsEventMap[K]>) {
    const arr = this.listeners[event] as ((...a: any[]) => void)[] | undefined;
    arr?.forEach((fn) => fn(...args));
  }

  connect(): void {
    if (this.destroyed) return;
    this.setStatus('connecting');
    this.clearTimers();

    const ws = new WebSocket(this.url);
    this.ws = ws;

    ws.on('open', () => {
      if (this.destroyed) { ws.close(); return; }
      this.retryCount = 0;
      this.setStatus('connected');
      this.startPing();
    });

    ws.on('message', (data) => {
      try {
        const msg = JSON.parse(data.toString()) as JiuwenMessage;
        this.emit('message', msg);
      } catch {
        // ignore malformed frames
      }
    });

    ws.on('error', (err) => {
      this.emit('error', err);
    });

    ws.on('close', () => {
      this.stopPing();
      if (this.destroyed) return;
      this.ws = null;
      this.scheduleReconnect();
    });
  }

  /** Force a fresh reconnect (used for "New session"). */
  reconnect(): void {
    if (this.destroyed) return;
    this.clearTimers();
    this.retryCount = 0;
    if (this.ws) {
      try { this.ws.close(); } catch { /* ignore */ }
      this.ws = null;
    }
    this.connect();
  }

  send(msg: JiuwenMessage): boolean {
    if (this.ws?.readyState !== WebSocket.OPEN) return false;
    this.ws.send(JSON.stringify(msg));
    return true;
  }

  disconnect(): void {
    this.destroyed = true;
    this.clearTimers();
    this.stopPing();
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
    this.setStatus('disconnected');
  }

  getStatus(): WsStatus { return this.status; }
  isConnected(): boolean { return this.status === 'connected'; }

  private setStatus(s: WsStatus): void {
    if (this.status === s) return;
    this.status = s;
    this.emit('status', s);
  }

  private scheduleReconnect(): void {
    const delay = BACKOFF_MS[Math.min(this.retryCount, BACKOFF_MS.length - 1)];
    this.retryCount++;
    this.setStatus('reconnecting');
    this.retryTimer = setTimeout(() => {
      if (!this.destroyed) this.connect();
    }, delay);
  }

  setPingInterval(ms: number): void {
    this.pingIntervalMs = Math.max(5_000, Math.min(ms, 300_000));
    if (this.status === 'connected') {
      this.stopPing();
      this.startPing();
    }
  }

  private startPing(): void {
    if (this.pingIntervalMs <= 0) return;
    this.pingTimer = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.ping();
      }
    }, this.pingIntervalMs);
  }

  private stopPing(): void {
    if (this.pingTimer) { clearInterval(this.pingTimer); this.pingTimer = null; }
  }

  private clearTimers(): void {
    if (this.retryTimer) { clearTimeout(this.retryTimer); this.retryTimer = null; }
  }
}
