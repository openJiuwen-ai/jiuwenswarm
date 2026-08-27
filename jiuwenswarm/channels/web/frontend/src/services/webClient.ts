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
import { resolveUserId } from '../utils/userId';
import i18n from '../i18n';
import { GoalRecord } from '../types/goal';
import { createSessionEventGate } from './sessionEventGate';

type EventHandler = (event: WsEvent) => void;
type TypedEventHandler<TPayload> = (event: WsEvent & { payload: TPayload }) => void;
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
  file_content: 'chat.file',
  tool_call: 'chat.tool_call',
  tool_update: 'chat.tool_update',
  tool_result: 'chat.tool_result',
  error: 'chat.error',
  interrupt_result: 'chat.interrupt_result',
  subtask_update: 'chat.subtask_update',
  ask_user_question: 'chat.ask_user_question',
  todo_update: 'todo.updated',
  session_update: 'session.updated',
  processing_status: 'chat.processing_status',
  heartbeat: 'connection.heartbeat',
  security_alert: 'security.alert',
  chat_retract: 'chat.retract',
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
  private readonly sessionEventGate = createSessionEventGate((event) => {
    this.dispatchEventNow(event);
  });

  getState(): WebConnectionState {
    return this.state;
  }

  onStateChange(handler: StateHandler): () => void {
    this.stateHandlers.add(handler);
    return () => {
      this.stateHandlers.delete(handler);
    };
  }

  on<TPayload = Record<string, unknown>>(
    eventName: string,
    handler: TypedEventHandler<TPayload>
  ): () => void {
    const set = this.handlers.get(eventName) ?? new Set<EventHandler>();
    const eventHandler = handler as EventHandler;
    set.add(eventHandler);
    this.handlers.set(eventName, set);

    return () => {
      const target = this.handlers.get(eventName);
      if (!target) {
        return;
      }
      target.delete(eventHandler);
      if (target.size === 0) {
        this.handlers.delete(eventName);
      }
    };
  }

  suspendSessionEvents(sessionId: string): () => void {
    return this.sessionEventGate.suspend(sessionId);
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
        // 1008 Policy Violation: gateway 鉴权失败 (token 失效/缺失)。
        // 重载页面, AppWithAuth 会探测 cookie 失效 -> 回到登录页。
        if (closeEvent.code === 1008) {
          this.updateState('closed');
          if (typeof window !== 'undefined') {
            window.location.reload();
          }
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
      ...(options.isStream ? { is_stream: true } : {}),
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

  /**
   * 发出去就不等了——给那些正常路径上压根不会有 res 的 is_stream 请求用（目前是 command.goal
   * 的 set/resume，见 backend-requests.md #4：process_stream/_chunk_to_message 对每个 chunk
   * 无条件 type="event"，没有能产出 res 的分支，连"需要用户确认"这类失败也是走
   * goal.confirm_required 事件）。跟 request() 共用同一套就绪检查（没连上照样立刻抛错，这个
   * 检查本身不依赖等 res），区别只是不注册 pending/不设超时——没有 res 要等，也就没有"超时"
   * 这回事，真实状态全靠调用方自己订阅事件拿。
   */
  async sendFireAndForget(
    method: string,
    params?: Record<string, unknown>,
    options: { isStream?: boolean } = {}
  ): Promise<void> {
    await this.ensureReady();

    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      throw this.createWebError(i18n.t('network.connectionUnavailable'), 'WS_NOT_READY', undefined, true);
    }

    const message: WsRequest = {
      type: 'req',
      id: this.generateRequestId(),
      method,
      params: params ?? {},
      ...(options.isStream ? { is_stream: true } : {}),
    };

    logDevWsTraffic({
      direction: 'outgoing',
      messageType: 'req',
      data: message,
    });
    this.ws.send(JSON.stringify(message));
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
        this.isRetriableCode(message.code),
        message.payload
      )
    );
  }

  private dispatchEvent(event: WsEvent): void {
    this.sessionEventGate.dispatch(event);
  }

  private dispatchEventNow(event: WsEvent): void {
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
    if (options.projectDir) params.set('project_dir', options.projectDir);
    // user_id 来自 URL ?user_id= 或 localStorage（见 utils/userId.ts），
    // 供 gateway 为 faas 注入 X-Session-Context（CreateSandbox 绑定用户标识）。
    // 浏览器 new WebSocket 无法设置自定义 header，只能走 query string。
    const userId = resolveUserId();
    if (userId) params.set('user_id', userId);
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
    retriable = false,
    payload?: unknown
  ): WebError {
    const error = new Error(message) as WebError;
    error.code = code;
    error.requestId = requestId;
    error.retriable = retriable;
    error.payload = payload;
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

interface GoalCommandResponsePayload {
  action?: string;
  message?: string;
  goal?: GoalRecord | null;
  record?: GoalRecord | null;
  cleared_goal?: GoalRecord | null;
  existing_goal?: GoalRecord | null;
  requested_objective?: string | null;
  code?: string | null;
}

/**
 * Goal（持续目标）控制里"有真正 res"的三个一次性动作：get/pause/clear。这三个走的是非流式
 * 一次性响应（interface.py 的 process_message()），正常应该秒回，用默认超时兜底——这里超时
 * 是真的有问题，不是误判。见 cjh/goal/Goal持续目标Web前端对接.md §4。
 * clear 成功后 goal 应视为已清空，不用 cleared_goal 兜底出一个"已清除的目标"。
 */
export async function requestGoalAction(params: {
  sessionId: string;
  action: 'get' | 'pause' | 'clear';
  /** 当前会话模式（如 'agent'），协议文档 v2 §2.1 要求带上，不要写死 'code.normal' */
  mode?: string;
}): Promise<GoalRecord | null> {
  const { sessionId, action, mode } = params;
  const payload = await webRequest<GoalCommandResponsePayload>('command.goal', {
    session_id: sessionId,
    action,
    mode: mode ?? 'agent',
  });
  if (action === 'clear') {
    return null;
  }
  return payload?.goal ?? payload?.record ?? null;
}

/**
 * set/resume：is_stream:true，抢到监听席位后正常路径上永远不会有 res——`process_stream`/
 * `_chunk_to_message`（gateway/message_handler/message_handler.py）对每个 chunk 无条件
 * `type="event"`，没有能产出 res 的分支；连"目标已存在，需要 overwrite_confirmed 确认"这类
 * 失败，协议里对应的也是 `goal.confirm_required` 事件，不是 res（见 backend-requests.md #4）。
 * 所以这两个动作干脆不等 res：发出去就返回，真实状态全靠 goal.snapshot/goal.updated 事件驱动
 * （`useWebSocket.ts` 的 `applyGoalSnapshot`），不再需要一个"等不到就超时"的 Promise。
 */
export async function sendGoalStreamCommand(params: {
  sessionId: string;
  action: 'set' | 'resume';
  objective?: string;
  mode?: string;
  modelName?: string | null;
}): Promise<void> {
  const { sessionId, action, objective, mode, modelName } = params;
  await webClient.sendFireAndForget(
    'command.goal',
    {
      session_id: sessionId,
      action,
      mode: mode ?? 'agent',
      ...(action === 'set' ? { objective, overwrite_confirmed: true } : {}),
      ...(modelName ? { model_name: modelName } : {}),
    },
    { isStream: true }
  );
}

// Expose webClient to window for debugging in development
if (import.meta.env.DEV) {
  (window as any).webClient = webClient;
}

export type { WsEvent, WebMessage };
