import {
  WebConnectOptions,
  WebConnectionState,
  WebError,
  WebMessage,
  WebRequestOptions,
  WsEvent,
  WsRequest,
  WsResponse,
} from '../types';
import { getWsBase } from '../utils/env';
import i18n from '../i18n';

type EventHandler = (event: WsEvent) => void;
type StateHandler = (state: WebConnectionState) => void;

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (reason?: unknown) => void;
  timeoutId: number;
}

const MAX_RECONNECT_ATTEMPTS = 5;
const DEFAULT_TIMEOUT_MS = 15000;

const LEGACY_EVENT_MAP: Record<string, string> = {
  connection_ack: 'connection.ack',
  content_chunk: 'chat.delta',
  content: 'chat.final',
  media_content: 'chat.media',
  tool_call: 'chat.tool_call',
  tool_result: 'chat.tool_result',
  error: 'chat.error',
  interrupt_result: 'chat.interrupt_result',
  subtask_update: 'chat.subtask_update',
  ask_user_question: 'chat.ask_user_question',
  todo_update: 'todo.updated',
  session_update: 'session.updated',
  processing_status: 'chat.processing_status',
  heartbeat: 'connection.heartbeat',
};

interface DevWsLogEntry {
  direction: 'outgoing' | 'incoming' | 'lifecycle';
  messageType?: 'req' | 'res' | 'event';
  data: unknown;
}

function logDevWsTraffic(entry: DevWsLogEntry): void {
  if (!import.meta.env.DEV) {
    return;
  }

  const body = {
    ...entry,
    at: new Date().toISOString(),
  };

  void fetch('/__dev/ws-log', {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
    },
    body: JSON.stringify(body),
    keepalive: true,
  }).catch(() => {
    // 仅用于本地调试日志，失败时不影响业务逻辑
  });
}

class WebClient {
  private ws: WebSocket | null = null;
  private state: WebConnectionState = 'idle';
  private handlers = new Map<string, Set<EventHandler>>();
  private stateHandlers = new Set<StateHandler>();
  private pending = new Map<string, PendingRequest>();
  private reconnectTimer: number | null = null;
  private reconnectAttempts = 0;
  private manualClose = false;
  private connectPromise: Promise<void> | null = null;
  private lastConnectOptions: WebConnectOptions = {};
  private requestSeq = 0;

  getState(): WebConnectionState {
    return this.state;
  }

  getInflightCount(): number {
    return this.pending.size;
  }

  onStateChange(handler: StateHandler): () => void {
    this.stateHandlers.add(handler);
    return () => {
      this.stateHandlers.delete(handler);
    };
  }

  on(eventName: string, handler: EventHandler): () => void {
    const set = this.handlers.get(eventName) ?? new Set<EventHandler>();
    set.add(handler);
    this.handlers.set(eventName, set);

    return () => {
      const target = this.handlers.get(eventName);
      if (!target) {
        return;
      }
      target.delete(handler);
      if (target.size === 0) {
        this.handlers.delete(eventName);
      }
    };
  }

  async connect(options: WebConnectOptions = {}): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN) {
      return;
    }
    if (this.connectPromise) {
      return this.connectPromise;
    }

    this.lastConnectOptions = options;
    this.manualClose = false;
    this.updateState(this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting');

    const url = this.buildWsUrl(options);

    this.connectPromise = new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(url);
      this.ws = ws;

      ws.onopen = () => {
        logDevWsTraffic({
          direction: 'lifecycle',
          data: { event: 'open', url },
        });
        this.reconnectAttempts = 0;
        this.updateState('ready');
        this.connectPromise = null;
        resolve();
      };

      ws.onmessage = (event) => {
        this.handleIncoming(event.data);
      };

      ws.onerror = () => {
        logDevWsTraffic({
          direction: 'lifecycle',
          data: { event: 'error' },
        });
        const error = this.createWebError(
          i18n.t('network.wsError'),
          'WS_ERROR',
          undefined,
          true
        );
        this.connectPromise = null;
        if (this.state !== 'ready') {
          reject(error);
        }
      };

      ws.onclose = (closeEvent) => {
        logDevWsTraffic({
          direction: 'lifecycle',
          data: {
            event: 'close',
            code: closeEvent.code,
            reason: closeEvent.reason,
            wasClean: closeEvent.wasClean,
          },
        });
        this.ws = null;
        this.connectPromise = null;
        this.rejectAllPending(
          this.createWebError(
            i18n.t('network.connectionClosedWithCode', { code: closeEvent.code }),
            'WS_DISCONNECTED',
            undefined,
            true
          )
        );
        if (this.manualClose || closeEvent.code === 1000) {
          this.updateState('closed');
          return;
        }
        this.scheduleReconnect();
      };
    });

    return this.connectPromise;
  }

  disconnect(reason = 'User disconnect'): Promise<void> {
    this.manualClose = true;
    this.clearReconnectTimer();
    this.rejectAllPending(
      this.createWebError(i18n.t('network.connectionClosed'), 'WS_CLOSED', undefined, false)
    );
    const currentWs = this.ws;
    let closedPromise = Promise.resolve();
    if (currentWs) {
      closedPromise = new Promise<void>((resolve) => {
        let finished = false;
        const finish = () => {
          if (finished) {
            return;
          }
          finished = true;
          resolve();
        };
        const timeoutId = window.setTimeout(() => {
          finish();
        }, 800);
        currentWs.addEventListener(
          'close',
          () => {
            window.clearTimeout(timeoutId);
            finish();
          },
          { once: true }
        );
        currentWs.close(1000, reason);
      });
    }
    this.ws = null;
    this.connectPromise = null;
    this.updateState('closed');
    return closedPromise;
  }

  async request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options: WebRequestOptions = {}
  ): Promise<T> {
    await this.ensureReady();

    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw this.createWebError(i18n.t('network.connectionUnavailable'), 'WS_NOT_READY', undefined, true);
    }

    const id = this.generateRequestId();
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const message: WsRequest = {
      type: 'req',
      id,
      method,
      params: params ?? {},
    };

    return new Promise<T>((resolve, reject) => {
      const timeoutId = window.setTimeout(() => {
        this.pending.delete(id);
        reject(this.createWebError(i18n.t('network.requestTimeout'), 'REQUEST_TIMEOUT', id, true));
      }, timeoutMs);

      const pending: PendingRequest = {
        resolve: (value) => resolve(value as T),
        reject,
        timeoutId,
      };
      this.pending.set(id, pending);

      if (options.signal) {
        const onAbort = () => {
          if (!this.pending.has(id)) {
            return;
          }
          window.clearTimeout(timeoutId);
          this.pending.delete(id);
          reject(this.createWebError(i18n.t('network.requestAborted'), 'REQUEST_ABORTED', id, false));
        };
        if (options.signal.aborted) {
          onAbort();
          return;
        }
        options.signal.addEventListener('abort', onAbort, { once: true });
      }

      logDevWsTraffic({
        direction: 'outgoing',
        messageType: 'req',
        data: message,
      });
      this.ws?.send(JSON.stringify(message));
    });
  }

  private async ensureReady(): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN && this.state === 'ready') {
      return;
    }
    await this.connect(this.lastConnectOptions);
  }

  private handleIncoming(rawData: string): void {
    let parsed: unknown;
    try {
      parsed = JSON.parse(rawData);
    } catch {
      logDevWsTraffic({
        direction: 'incoming',
        data: { rawData, parse: 'failed' },
      });
      return;
    }

    const message = this.normalizeIncoming(parsed);
    if (!message) {
      logDevWsTraffic({
        direction: 'incoming',
        data: { parsed, normalize: 'ignored' },
      });
      return;
    }

    logDevWsTraffic({
      direction: 'incoming',
      messageType: message.type,
      data: message,
    });

    if (message.type === 'res') {
      this.resolvePending(message);
      return;
    }

    this.dispatchEvent(message);
  }

  private normalizeIncoming(input: unknown): WsResponse | WsEvent | null {
    if (!input || typeof input !== 'object') {
      return null;
    }
    const msg = input as Record<string, unknown>;
    const rawType = msg.type;
    if (rawType === 'res') {
      if (typeof msg.id !== 'string') {
        return null;
      }
      return {
        type: 'res',
        id: msg.id,
        ok: Boolean(msg.ok),
        payload: msg.payload,
        error: typeof msg.error === 'string' ? msg.error : undefined,
        code: typeof msg.code === 'string' ? msg.code : undefined,
      };
    }

    if (rawType === 'event') {
      const eventName = typeof msg.event === 'string' ? msg.event : '';
      if (!eventName) {
        return null;
      }
      return {
        type: 'event',
        event: eventName,
        payload: this.normalizePayload(msg.payload),
        seq: typeof msg.seq === 'number' ? msg.seq : undefined,
        stream_id: typeof msg.stream_id === 'string' ? msg.stream_id : undefined,
      };
    }

    if (typeof rawType === 'string') {
      const mappedEvent = LEGACY_EVENT_MAP[rawType];
      if (!mappedEvent) {
        return null;
      }
      return {
        type: 'event',
        event: mappedEvent,
        payload: this.normalizePayload(msg.payload),
      };
    }

    return null;
  }

  private normalizePayload(payload: unknown): Record<string, unknown> {
    if (!payload || typeof payload !== 'object') {
      return {};
    }
    return payload as Record<string, unknown>;
  }

  private resolvePending(message: WsResponse): void {
    const pending = this.pending.get(message.id);
    if (!pending) {
      return;
    }
    window.clearTimeout(pending.timeoutId);
    this.pending.delete(message.id);

    if (message.ok) {
      pending.resolve(message.payload);
      return;
    }

    pending.reject(
      this.createWebError(
        message.error ?? i18n.t('network.requestFailed'),
        message.code,
        message.id,
        this.isRetriableCode(message.code)
      )
    );
  }

  private dispatchEvent(event: WsEvent): void {
    const handlers = this.handlers.get(event.event);
    if (!handlers || handlers.size === 0) {
      return;
    }
    handlers.forEach((handler) => {
      handler(event);
    });
  }

  private scheduleReconnect(): void {
    this.clearReconnectTimer();
    this.reconnectAttempts += 1;
    this.updateState('reconnecting');

    // 前 N 次使用指数退避，超过后改为固定间隔持续重试，后端恢复后能自动检测并恢复连接
    const delay =
      this.reconnectAttempts <= MAX_RECONNECT_ATTEMPTS
        ? Math.min(1000 * 2 ** (this.reconnectAttempts - 1), 30000)
        : 2000; // 每 2 秒持续尝试

    this.reconnectTimer = window.setTimeout(() => {
      void this.connect(this.lastConnectOptions);
    }, delay);
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimer !== null) {
      window.clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
  }

  private rejectAllPending(error: WebError): void {
    this.pending.forEach((entry) => {
      window.clearTimeout(entry.timeoutId);
      entry.reject(error);
    });
    this.pending.clear();
  }

  private updateState(state: WebConnectionState): void {
    this.state = state;
    this.stateHandlers.forEach((handler) => {
      handler(state);
    });
  }

  private buildWsUrl(options: WebConnectOptions): string {
    const wsBase = getWsBase();
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.host;
    const base = wsBase || `${protocol}//${host}`;
    const path = base.endsWith('/ws') || base.endsWith('/ws/gateway') ? '' : '/ws';
    const params = new URLSearchParams();
    if (options.provider) params.set('provider', options.provider);
    if (options.apiKey) params.set('api_key', options.apiKey);
    if (options.apiBase) params.set('api_base', options.apiBase);
    if (options.model) params.set('model', options.model);
    if (options.projectPath) params.set('project_path', options.projectPath);
    const query = params.toString();
    const target = `${base}${path}`;
    return query ? `${target}?${query}` : target;
  }

  private generateRequestId(): string {
    this.requestSeq += 1;
    const stamp = Date.now().toString(36);
    return `req_${stamp}_${this.requestSeq}`;
  }

  private createWebError(
    message: string,
    code?: string,
    requestId?: string,
    retriable = false
  ): WebError {
    const error = new Error(message) as WebError;
    error.code = code;
    error.requestId = requestId;
    error.retriable = retriable;
    return error;
  }

  private isRetriableCode(code?: string): boolean {
    return (
      code === 'REQUEST_TIMEOUT' ||
      code === 'WS_DISCONNECTED' ||
      code === 'WS_NOT_READY'
    );
  }
}

export const webClient = new WebClient();

export async function webRequest<T = unknown>(
  method: string,
  params?: Record<string, unknown>,
  options?: WebRequestOptions
): Promise<T> {
  return webClient.request<T>(method, params, options);
}

export type { WsEvent, WebMessage };
