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
import { getWebTransport, getWsBase } from '../utils/env';
import { resolveUserId } from '../utils/userId';
import { WebHttpClient } from './webHttpClient';
import i18n from '../i18n';
import { appendRuntimeScopeQuery } from './runtimeScope';
import { withExpertUiLanguage } from './expertUiLanguage';
import { GoalRecord } from '../types/goal';
import { createSessionEventGate } from './sessionEventGate';
import { RuntimeResume } from './runtimeResume';

type EventHandler = (event: WsEvent) => void;
type TypedEventHandler<TPayload> = (event: WsEvent & { payload: TPayload }) => void;
type StateHandler = (state: WebConnectionState) => void;

interface PendingRequest {
  resolve: (value: unknown) => void;
  reject: (reason?: unknown) => void;
  timeoutId: number;
  awaitRuntimeAccepted: boolean;
}

const MAX_RECONNECT_ATTEMPTS = 5;
const DEFAULT_TIMEOUT_MS = 15000;
const MAX_RECOVERY_ATTEMPTS = 3;

/** 暂停期间暂存、恢复后回放的流式事件 */
export const PAUSABLE_STREAM_EVENTS = new Set([
  'chat.delta', 'chat.final', 'chat.media', 'chat.file',
  'chat.tool_call', 'chat.tool_result', 'chat.tool_update',
  'chat.reasoning',
  'todo.updated', 'context.compressed', 'context.usage',
  'chat.subtask_update', 'chat.subagent_activity', 'chat.usage_summary',
]);

export interface PauseBufferHook {
  isActive: () => boolean;
  onBuffer: (event: WsEvent) => void;
}

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
  private readonly runtimeResume = new RuntimeResume();
  private resumeOwner = '';
  private connectedOnce = false;
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

  on<TPayload = Record<string, unknown>>(eventName: string, handler: TypedEventHandler<TPayload>): () => void {
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
    const owner = resolveUserId() || '';
    if (owner !== this.resumeOwner) this.runtimeResume.clear();
    this.resumeOwner = owner;
    this.manualClose = false;
    this.updateState(this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting');

    const url = this.buildWsUrl(options);

    this.connectPromise = new Promise<void>((resolve, reject) => {
      const ws = new WebSocket(url);
      this.ws = ws;
      const rejectClosedConnection = () =>
        reject(this.createWebError(i18n.t('network.connectionClosed'), 'WS_CLOSED', undefined, false));

      ws.onopen = () => {
        if (this.ws !== ws) {
          rejectClosedConnection();
          return;
        }
        logDevWsTraffic({
          direction: 'lifecycle',
          data: { event: 'open', url },
        });
        this.reconnectAttempts = 0;
        this.updateState('ready');
        this.connectPromise = null;
        resolve();
        if (this.connectedOnce) void this.resumeExecutions();
        this.connectedOnce = true;
      };

      ws.onmessage = (event) => {
        if (this.ws !== ws) return;
        this.handleIncoming(event.data);
      };

      ws.onerror = () => {
        if (this.ws !== ws) {
          rejectClosedConnection();
          return;
        }
        logDevWsTraffic({
          direction: 'lifecycle',
          data: { event: 'error' },
        });
        const error = this.createWebError(i18n.t('network.wsError'), 'WS_ERROR', undefined, true);
        this.connectPromise = null;
        if (this.state !== 'ready') {
          reject(error);
        }
      };

      ws.onclose = (closeEvent) => {
        if (this.ws !== ws) {
          rejectClosedConnection();
          return;
        }
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
            true,
          ),
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

  /**
   * 断开后立刻用同一组参数重连。
   *
   * 登录态变化时必须调这个：登录会话 id 是 **WS 握手时**从 cookie / 请求头读的，
   * 之后这条连接就一直用那份值。而登录走的是 HTTP，发生在握手之后——不重连的话，
   * 连接上停留的还是登录前那份，服务端据此取凭据会取不到，
   * 表现为"选了免费模型却跑了配置的模型"。
   */
  async reconnect(reason = 'Auth changed'): Promise<void> {
    const options = this.lastConnectOptions;
    await this.disconnect(reason);
    await this.connect(options);
  }

  disconnect(reason = 'User disconnect'): Promise<void> {
    this.manualClose = true;
    this.clearReconnectTimer();
    this.rejectAllPending(this.createWebError(i18n.t('network.connectionClosed'), 'WS_CLOSED', undefined, false));
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
          { once: true },
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
    options: WebRequestOptions = {},
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
      params: withExpertUiLanguage(method, params),
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
        awaitRuntimeAccepted: options.awaitRuntimeAccepted === true,
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
      try {
        options.onRequestId?.(id);
        this.ws!.send(JSON.stringify(message));
      } catch (error) {
        window.clearTimeout(timeoutId);
        this.pending.delete(id);
        reject(error);
      }
    }).then(async (value) => {
      if (method === 'session.get_metadata' && !params?.runtime_resume && typeof params?.session_id === 'string') {
        const runtime = (value as { runtime?: Record<string, unknown> } | null)?.runtime;
        if (
          runtime?.recovery_pending === true ||
          (runtime?.is_processing === true && typeof runtime.worker_instance === 'string')
        ) {
          // A freshly loaded page has no stream cursor yet. Reattach to the
          // existing execution through its snapshot, never by sending input.
          await this.resumeExecutions([
            {
              session_id: params.session_id,
              runtime_resume: {
                worker_instance: typeof runtime.worker_instance === 'string' ? runtime.worker_instance : '',
                after: 0,
              },
            },
          ]);
        }
      }
      return value;
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
    options: { isStream?: boolean } = {},
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

    if (!this.runtimeResume.observe(message)) return;
    this.resolveRuntimeAcceptedPending(message);
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
      const payload = this.normalizePayload(msg.payload);
      return {
        type: 'event',
        // Older gateways wrap control ACKs in chat.final; an ACK must never close a turn.
        event: eventName === 'chat.final' && payload.event_type === 'runtime.accepted' ? 'runtime.accepted' : eventName,
        payload,
        seq: typeof msg.seq === 'number' ? msg.seq : undefined,
        stream_id: typeof msg.stream_id === 'string' ? msg.stream_id : undefined,
      };
    }

    if (typeof rawType === 'string') {
      const mappedEvent = LEGACY_EVENT_MAP[rawType];
      if (!mappedEvent) {
        return null;
      }
      const payload = this.normalizePayload(msg.payload);
      return {
        type: 'event',
        event:
          mappedEvent === 'chat.final' && payload.event_type === 'runtime.accepted' ? 'runtime.accepted' : mappedEvent,
        payload,
      };
    }

    return null;
  }

  private async resumeExecutions(queries = this.runtimeResume.queries()): Promise<void> {
    await Promise.all(queries.map((query) => this.resumeExecution(query)));
  }

  private async resumeExecution(query: ReturnType<RuntimeResume['queries']>[number]): Promise<void> {
    const socket = this.ws;
    const owner = this.resumeOwner;
    for (let attempt = 0; attempt < MAX_RECOVERY_ATTEMPTS; attempt += 1) {
      if (this.ws !== socket || this.resumeOwner !== owner || this.state !== 'ready') return;
      try {
        const metadata = await this.request<{ runtime?: Record<string, unknown> }>('session.get_metadata', query, {
          timeoutMs: 10_000,
        });
        if (this.ws !== socket || this.resumeOwner !== owner) return;
        const runtime = metadata?.runtime;
        if (!runtime) return;
        this.dispatchEvent({
          type: 'event',
          event: 'runtime.snapshot',
          payload: { session_id: query.session_id, ...runtime },
        });
        if (runtime.recovery_pending === true) {
          // An unfinished snapshot cannot clear a running task. Retry only the
          // metadata/cursor query, preserving the original accepted execution.
          if (attempt + 1 < MAX_RECOVERY_ATTEMPTS) {
            const delay =
              typeof runtime.retry_after_ms === 'number' && Number.isFinite(runtime.retry_after_ms)
                ? Math.max(100, Math.min(1000, runtime.retry_after_ms))
                : 300;
            await new Promise<void>((resolve) => window.setTimeout(resolve, delay));
            continue;
          }
          return;
        }
        if (typeof runtime.is_processing === 'boolean') {
          this.dispatchEvent({
            type: 'event',
            event: 'chat.processing_status',
            payload: { session_id: query.session_id, is_processing: runtime.is_processing },
          });
        }
        if (runtime.interrupted === true) {
          this.dispatchEvent({
            type: 'event',
            event: 'chat.error',
            payload: {
              session_id: query.session_id,
              code: 'WORKER_EXECUTION_INTERRUPTED',
              error: i18n.t('network.workerExecutionInterrupted'),
              recoverable: false,
            },
          });
        }
        if (runtime.history_required === true) {
          const dispatches = Array.isArray(runtime.dispatches) ? runtime.dispatches : [];
          for (const dispatch of dispatches) {
            const interactions = Array.isArray(dispatch.pending_interactions) ? dispatch.pending_interactions : [];
            for (const interaction of interactions) {
              if (typeof interaction.event_type === 'string') {
                this.dispatchEvent({
                  type: 'event',
                  event: interaction.event_type,
                  payload: { ...interaction, session_id: query.session_id },
                });
              }
            }
          }
          this.dispatchEvent({
            type: 'event',
            event: 'runtime.history_required',
            payload: { session_id: query.session_id },
          });
        }
        return;
      } catch {
        // The next reconnect/history query reconciles state. Never retry chat.send.
        return;
      }
    }
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
    if (message.ok && pending.awaitRuntimeAccepted) {
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
        message.payload,
      ),
    );
  }

  private resolveRuntimeAcceptedPending(message: WsEvent): void {
    if (message.event !== 'runtime.accepted' && message.event !== 'chat.error') {
      return;
    }
    const requestId = message.payload.request_id;
    if (typeof requestId !== 'string') {
      return;
    }
    const pending = this.pending.get(requestId);
    if (!pending?.awaitRuntimeAccepted) {
      return;
    }
    window.clearTimeout(pending.timeoutId);
    this.pending.delete(requestId);
    if (message.event === 'runtime.accepted') {
      pending.resolve(message.payload);
      return;
    }
    const error = typeof message.payload.error === 'string' ? message.payload.error : i18n.t('network.requestFailed');
    const code = typeof message.payload.code === 'string' ? message.payload.code : undefined;
    pending.reject(this.createWebError(error, code, requestId, true, message.payload));
  }

  private dispatchEvent(event: WsEvent): void {
    this.sessionEventGate.dispatch(event);
  }

  /** 把定时任务监听流上的帧送进现有 chat.* 处理。长连接模式用不到。 */
  injectEvent(event: WsEvent): void {
    this.dispatchEvent(event);
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
    // 浏览器 new WebSocket 无法设置自定义 header，身份走 query：
    // user_id / group_id / bot_id（见 runtimeScope.ts；个人版可仅有 user_id）。
    appendRuntimeScopeQuery(params);
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
    payload?: unknown,
  ): WebError {
    const error = new Error(message) as WebError;
    error.code = code;
    error.requestId = requestId;
    error.retriable = retriable;
    error.payload = payload;
    return error;
  }

  private isRetriableCode(code?: string): boolean {
    return code === 'REQUEST_TIMEOUT' || code === 'WS_DISCONNECTED' || code === 'WS_NOT_READY';
  }
}

export const webClient =
  getWebTransport() === 'http' ? new WebHttpClient() : new WebClient();

export async function webRequest<T = unknown>(
  method: string,
  params?: Record<string, unknown>,
  options?: WebRequestOptions,
): Promise<T> {
  return webClient.request<T>(method, params, options);
}

// ── SwarmFlow workflow 分页 RPC 封装（command.workflows） ─────────

export interface WorkflowListResponse {
  type?: string;
  workflows?: unknown[];
  session_id?: string;
  total?: number;
  has_more?: boolean;
}

export interface WorkflowDetailResponse {
  type?: string;
  workflow?: unknown;
  session_id?: string;
  phase_total?: number;
  has_more?: boolean;
}

export interface WorkflowPhaseResponse {
  type?: string;
  phase?: unknown;
  session_id?: string;
  agent_total?: number;
  has_more?: boolean;
  error?: unknown;
}

export interface WorkflowAgentResponse {
  type?: string;
  agent?: unknown;
  session_id?: string;
  error?: unknown;
}

export async function requestWorkflowList(
  sessionId: string,
  offset = 0,
  limit?: number,
): Promise<WorkflowListResponse> {
  return webRequest<WorkflowListResponse>('command.workflows', {
    session_id: sessionId,
    action: 'list',
    offset,
    ...(limit == null ? {} : { limit }),
  });
}

export async function requestWorkflowDetail(
  sessionId: string,
  workflowId: string,
  phaseOffset = 0,
  phaseLimit?: number,
): Promise<WorkflowDetailResponse> {
  return webRequest<WorkflowDetailResponse>('command.workflows', {
    session_id: sessionId,
    action: 'get_workflow',
    workflow_id: workflowId,
    phase_offset: phaseOffset,
    ...(phaseLimit == null ? {} : { phase_limit: phaseLimit }),
  });
}

export async function requestPhaseAgents(
  sessionId: string,
  workflowId: string,
  phaseId: string,
  agentOffset = 0,
  agentLimit?: number,
): Promise<WorkflowPhaseResponse> {
  return webRequest<WorkflowPhaseResponse>('command.workflows', {
    session_id: sessionId,
    action: 'get_phase',
    workflow_id: workflowId,
    phase_id: phaseId,
    agent_offset: agentOffset,
    ...(agentLimit == null ? {} : { agent_limit: agentLimit }),
  });
}

export async function requestAgentDetail(
  sessionId: string,
  workflowId: string,
  phaseId: string,
  agentId: string,
): Promise<WorkflowAgentResponse> {
  return webRequest<WorkflowAgentResponse>('command.workflows', {
    session_id: sessionId,
    action: 'get_agent',
    workflow_id: workflowId,
    phase_id: phaseId,
    agent_id: agentId,
  });
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
  modelParams?: Record<string, unknown>;
}): Promise<void> {
  const { sessionId, action, objective, mode, modelName, modelParams } = params;
  const outgoingModel =
    modelParams && Object.keys(modelParams).length > 0
      ? modelParams
      : modelName
        ? { model_name: modelName }
        : {};
  await webClient.sendFireAndForget(
    'command.goal',
    {
      session_id: sessionId,
      action,
      mode: mode ?? 'agent',
      ...(action === 'set' ? { objective, overwrite_confirmed: true } : {}),
      ...outgoingModel,
    },
    { isStream: true },
  );
}

// Expose webClient to window for debugging in development
if (import.meta.env.DEV) {
  (window as any).webClient = webClient;
}

export type { WsEvent, WebMessage };
