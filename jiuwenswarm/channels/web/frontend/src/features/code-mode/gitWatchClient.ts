import type { WebConnectionState, WebError, WsEvent, WsResponse } from '../../types';
import { getWsBase } from '../../utils/env';

type EventHandler = (event: WsEvent) => void;
type StateHandler = (state: WebConnectionState) => void;

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (reason?: unknown) => void;
  timeoutId: number;
}

const DEFAULT_TIMEOUT_MS = 15_000;
const MAX_RECONNECT_DELAY_MS = 30_000;

function buildGitWsUrl(): string {
  const configuredBase = getWsBase();
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const base = configuredBase || `${protocol}//${window.location.host}`;
  const [baseWithoutQuery] = base.split('?', 1);
  const root = baseWithoutQuery.replace(/\/+$/, '').replace(/\/ws(?:\/gateway)?$/, '');
  return `${root}/ws/git`;
}

class GitWatchClient {
  private ws: WebSocket | null = null;
  private state: WebConnectionState = 'idle';
  private connectPromise: Promise<void> | null = null;
  private pending = new Map<string, PendingRequest>();
  private eventHandlers = new Map<string, Set<EventHandler>>();
  private stateHandlers = new Set<StateHandler>();
  private reconnectTimer: number | null = null;
  private reconnectAttempts = 0;
  private requestSequence = 0;
  private manualClose = false;

  getState(): WebConnectionState {
    return this.state;
  }

  onStateChange(handler: StateHandler): () => void {
    this.stateHandlers.add(handler);
    return () => this.stateHandlers.delete(handler);
  }

  on(eventName: string, handler: EventHandler): () => void {
    const handlers = this.eventHandlers.get(eventName) ?? new Set<EventHandler>();
    handlers.add(handler);
    this.eventHandlers.set(eventName, handlers);
    return () => {
      const current = this.eventHandlers.get(eventName);
      current?.delete(handler);
      if (current?.size === 0) this.eventHandlers.delete(eventName);
    };
  }

  async connect(): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) return;
    if (this.connectPromise) return this.connectPromise;

    this.manualClose = false;
    this.updateState(this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting');
    this.connectPromise = new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(buildGitWsUrl());
      this.ws = ws;

      ws.onopen = () => {
        this.connectPromise = null;
        this.reconnectAttempts = 0;
        this.updateState('ready');
        resolve();
      };

      ws.onmessage = event => this.handleIncoming(event.data);

      ws.onerror = () => {
        const error = this.createError('Git WebSocket connection failed', 'WS_ERROR', true);
        if (this.state !== 'ready') {
          this.connectPromise = null;
          reject(error);
        }
      };

      ws.onclose = event => {
        this.ws = null;
        this.connectPromise = null;
        this.rejectAllPending(this.createError(`Git WebSocket closed (${event.code})`, 'WS_DISCONNECTED', true));
        if (this.manualClose || event.code === 1000) {
          this.updateState('closed');
          return;
        }
        this.scheduleReconnect();
      };
    });

    return this.connectPromise;
  }

  disconnect(reason = 'Git diff watcher unused'): void {
    this.manualClose = true;
    this.clearReconnectTimer();
    this.rejectAllPending(this.createError('Git WebSocket closed', 'WS_CLOSED', false));
    this.ws?.close(1000, reason);
    this.ws = null;
    this.connectPromise = null;
    this.updateState('closed');
  }

  async request<T>(method: string, params: Record<string, unknown>, options: { timeoutMs?: number } = {}): Promise<T> {
    await this.connect();
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw this.createError('Git WebSocket is unavailable', 'WS_NOT_READY', true);
    }

    const id = this.nextRequestId();
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    return new Promise<T>((resolve, reject) => {
      const timeoutId = window.setTimeout(() => {
        this.pending.delete(id);
        reject(this.createError('Git WebSocket request timed out', 'REQUEST_TIMEOUT', true));
      }, timeoutMs);
      this.pending.set(id, {
        resolve: value => resolve(value as T),
        reject,
        timeoutId,
      });
      this.ws?.send(JSON.stringify({ type: 'req', id, method, params }));
    });
  }

  private handleIncoming(rawData: string): void {
    let message: unknown;
    try {
      message = JSON.parse(rawData);
    } catch {
      return;
    }
    if (!message || typeof message !== 'object') return;
    const raw = message as Record<string, unknown>;
    if (raw.type === 'res' && typeof raw.id === 'string') {
      this.resolvePending({
        type: 'res',
        id: raw.id,
        ok: Boolean(raw.ok),
        payload: raw.payload,
        error: typeof raw.error === 'string' ? raw.error : undefined,
        code: typeof raw.code === 'string' ? raw.code : undefined,
      });
      return;
    }
    if (raw.type !== 'event' || typeof raw.event !== 'string') return;
    const payload = raw.payload && typeof raw.payload === 'object' ? (raw.payload as Record<string, unknown>) : {};
    const event: WsEvent = {
      type: 'event',
      event: raw.event,
      payload,
      seq: typeof raw.seq === 'number' ? raw.seq : undefined,
    };
    this.eventHandlers.get(event.event)?.forEach(handler => handler(event));
  }

  private resolvePending(response: WsResponse): void {
    const pending = this.pending.get(response.id);
    if (!pending) return;
    window.clearTimeout(pending.timeoutId);
    this.pending.delete(response.id);
    if (response.ok) {
      pending.resolve(response.payload);
      return;
    }
    const error = this.createError(response.error || 'Git request failed', response.code, false);
    error.requestId = response.id;
    pending.reject(error);
  }

  private scheduleReconnect(): void {
    this.clearReconnectTimer();
    this.reconnectAttempts += 1;
    this.updateState('reconnecting');
    const delay = Math.min(1000 * 2 ** Math.min(this.reconnectAttempts - 1, 5), MAX_RECONNECT_DELAY_MS);
    this.reconnectTimer = window.setTimeout(() => {
      this.reconnectTimer = null;
      void this.connect().catch(() => undefined);
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer === null) return;
    window.clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
  }

  private rejectAllPending(error: WebError): void {
    this.pending.forEach(pending => {
      window.clearTimeout(pending.timeoutId);
      pending.reject(error);
    });
    this.pending.clear();
  }

  private updateState(state: WebConnectionState): void {
    this.state = state;
    this.stateHandlers.forEach(handler => handler(state));
  }

  private nextRequestId(): string {
    this.requestSequence += 1;
    return `git_req_${Date.now().toString(36)}_${this.requestSequence}`;
  }

  private createError(message: string, code?: string, retriable = false): WebError {
    const error = new Error(message) as WebError;
    error.code = code;
    error.retriable = retriable;
    return error;
  }
}

export const gitWatchClient = new GitWatchClient();
