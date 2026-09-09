/**
 * 北向 A2 HTTP/SSE 客户端（``getWebTransport()==='http'``：显式 transport 或企业默认）。
 * 对外口与 WebClient 相同：connect / request / on。映射与泵流只发生在这里。
 */
import {
  WebConnectOptions,
  WebConnectionState,
  WebError,
  WebRequestOptions,
  WsEvent,
} from '../types';
import { getGatewayHttpBase } from '../utils/env';
import { isEnterprise } from '../edition';
import {
  hasManagerSessionCredentials,
  managerAuthenticatedFetch,
} from '../auth/manager/authSession';
import i18n from '../i18n';
import { buildRuntimeIdentityHeaders } from './runtimeScope';
import { PauseBufferHook, PAUSABLE_STREAM_EVENTS } from './webClient';

type EventHandler = (event: WsEvent) => void;
type TypedEventHandler<TPayload> = (event: WsEvent & { payload: TPayload }) => void;
type StateHandler = (state: WebConnectionState) => void;

const MAX_RECONNECT_ATTEMPTS = 5;
const DEFAULT_TIMEOUT_MS = 15000;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function pickHeaderString(value: unknown): string | undefined {
  if (typeof value !== 'string') {
    return undefined;
  }
  const normalized = value.trim();
  return normalized || undefined;
}

type HttpVerb = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
type RestKind = 'unary' | 'sse' | 'history-stream';

export class RestAssemblyError extends Error {
  readonly missing: string[];

  constructor(message: string, missing: string[] = []) {
    super(message);
    this.name = 'RestAssemblyError';
    this.missing = missing;
  }
}

interface AssembledWebRest {
  method: string;
  verb: HttpVerb;
  url: string;
  query: Record<string, string> | null;
  jsonBody: Record<string, unknown> | null;
  kind: RestKind;
}

interface RouteRow {
  verb: HttpVerb;
  path: string;
  kind: RestKind;
}

const ROUTES: Record<string, RouteRow> = {
  'connection.status': { verb: 'GET', path: '/connection/status', kind: 'unary' },
  'session.list': { verb: 'GET', path: '/sessions', kind: 'unary' },
  'session.create': { verb: 'POST', path: '/sessions', kind: 'unary' },
  'session.get_metadata': { verb: 'GET', path: '/sessions/{session_id}', kind: 'unary' },
  'session.rename': { verb: 'PATCH', path: '/sessions/{session_id}', kind: 'unary' },
  'session.pin': { verb: 'PATCH', path: '/sessions/{session_id}', kind: 'unary' },
  'session.delete': { verb: 'DELETE', path: '/sessions/{session_id}', kind: 'unary' },
  'history.get': { verb: 'GET', path: '/sessions/{session_id}/history', kind: 'history-stream' },
  'chat.send': { verb: 'POST', path: '/chat/completions', kind: 'sse' },
  'chat.interrupt': { verb: 'POST', path: '/chat/{session_id}/actions/interrupt', kind: 'unary' },
  'chat.user_answer': { verb: 'POST', path: '/chat/{session_id}/actions/answer', kind: 'unary' },
  'config.get': { verb: 'GET', path: '/config', kind: 'unary' },
  'models.list': { verb: 'GET', path: '/models', kind: 'unary' },
  'a2a.outbound.list': { verb: 'GET', path: '/a2a/outbound/agents', kind: 'unary' },
  'a2a.outbound.enabled.update': { verb: 'PATCH', path: '/a2a/outbound/agents/{agent_id}/enabled', kind: 'unary' },
  'a2a.outbound.dispatch.list': { verb: 'GET', path: '/a2a/outbound/dispatches', kind: 'unary' },
  'locale.get_conf': { verb: 'GET', path: '/locale', kind: 'unary' },
  'locale.set_conf': { verb: 'PUT', path: '/locale', kind: 'unary' },
  'cron.job.list': { verb: 'GET', path: '/cron/jobs', kind: 'unary' },
  'cron.job.create': { verb: 'POST', path: '/cron/jobs', kind: 'unary' },
  'cron.job.meta': { verb: 'GET', path: '/cron/jobs/meta', kind: 'unary' },
  'cron.job.get': { verb: 'GET', path: '/cron/jobs/{id}', kind: 'unary' },
  'cron.job.update': { verb: 'PATCH', path: '/cron/jobs/{id}', kind: 'unary' },
  'cron.job.delete': { verb: 'DELETE', path: '/cron/jobs/{id}', kind: 'unary' },
  'cron.job.toggle': { verb: 'POST', path: '/cron/jobs/{id}/actions/toggle', kind: 'unary' },
  'cron.job.preview': { verb: 'POST', path: '/cron/jobs/{id}/actions/preview', kind: 'unary' },
  'cron.job.run_now': { verb: 'POST', path: '/cron/jobs/{id}/actions/run-now', kind: 'unary' },
  'skills.list': { verb: 'GET', path: '/skills', kind: 'unary' },
  'skills.installed': { verb: 'GET', path: '/skills/installed', kind: 'unary' },
  'skills.get': { verb: 'GET', path: '/skills/{name}', kind: 'unary' },
  'skills.install': { verb: 'POST', path: '/skills/actions/install', kind: 'unary' },
  'skills.uninstall': { verb: 'POST', path: '/skills/actions/uninstall', kind: 'unary' },
  'skills.toggle': { verb: 'POST', path: '/skills/actions/toggle', kind: 'unary' },
  'skills.import_local': { verb: 'POST', path: '/skills/actions/import-local', kind: 'unary' },
  'skills.source.providers': { verb: 'GET', path: '/skills/sources', kind: 'unary' },
  'skills.source.search': { verb: 'GET', path: '/skills/sources/search', kind: 'unary' },
  'skills.source.install': { verb: 'POST', path: '/skills/sources/actions/install', kind: 'unary' },
  'skills.updates.check': { verb: 'GET', path: '/skills/updates', kind: 'unary' },
  'skills.update': { verb: 'POST', path: '/skills/actions/update', kind: 'unary' },
  'skills.teamskillshub.info': { verb: 'GET', path: '/skills/teamskillshub', kind: 'unary' },
  'skills.retrieval.status': { verb: 'GET', path: '/skills/retrieval/status', kind: 'unary' },
  'skills.retrieval.tree': { verb: 'GET', path: '/skills/retrieval/tree', kind: 'unary' },
  'skills.retrieval.index_build': { verb: 'POST', path: '/skills/retrieval/actions/index-build', kind: 'unary' },
  'skills.retrieval.index_cancel': { verb: 'POST', path: '/skills/retrieval/actions/index-cancel', kind: 'unary' },
  'skills.enterprise.list': { verb: 'GET', path: '/skills/enterprise', kind: 'unary' },
  'skills.enterprise.install': { verb: 'POST', path: '/skills/enterprise/actions/install', kind: 'unary' },
  'skills.enterprise.uninstall': { verb: 'POST', path: '/skills/enterprise/actions/uninstall', kind: 'unary' },
  'skills.marketplace.list': { verb: 'GET', path: '/skills/marketplace', kind: 'unary' },
  'skills.marketplace.add': { verb: 'POST', path: '/skills/marketplace', kind: 'unary' },
  'skills.marketplace.remove': { verb: 'POST', path: '/skills/marketplace/actions/remove', kind: 'unary' },
  'skills.marketplace.toggle': { verb: 'POST', path: '/skills/marketplace/actions/toggle', kind: 'unary' },
  'skills.clawhub.get_token': { verb: 'GET', path: '/skills/clawhub/token', kind: 'unary' },
  'skills.clawhub.set_token': { verb: 'PUT', path: '/skills/clawhub/token', kind: 'unary' },
  'skills.clawhub.search': { verb: 'GET', path: '/skills/clawhub/search', kind: 'unary' },
  'skills.clawhub.download': { verb: 'POST', path: '/skills/clawhub/actions/download', kind: 'unary' },
  'skills.skillnet.search': { verb: 'POST', path: '/skills/skillnet/actions/search', kind: 'unary' },
  'skills.skillnet.install': { verb: 'POST', path: '/skills/skillnet/actions/install', kind: 'unary' },
  'skills.skillnet.install_status': { verb: 'GET', path: '/skills/skillnet/install-status', kind: 'unary' },
  'skills.skillnet.evaluate': { verb: 'POST', path: '/skills/skillnet/actions/evaluate', kind: 'unary' },
  'skills.evolution.get': { verb: 'GET', path: '/skills/evolution', kind: 'unary' },
  'skills.evolution.save': { verb: 'PUT', path: '/skills/evolution', kind: 'unary' },
  'project.list': { verb: 'GET', path: '/projects', kind: 'unary' },
  'project.info': { verb: 'GET', path: '/projects/{project_id}', kind: 'unary' },
  'project.get_sessions': { verb: 'GET', path: '/projects/{project_id}/sessions', kind: 'unary' },
  'project.get_cron_sessions': { verb: 'GET', path: '/projects/{project_id}/cron-sessions', kind: 'unary' },
  'project.create': { verb: 'POST', path: '/projects', kind: 'unary' },
  'project.rename': { verb: 'PATCH', path: '/projects/{project_id}', kind: 'unary' },
  'project.pin': { verb: 'POST', path: '/projects/{project_id}/actions/pin', kind: 'unary' },
  'project.remove': { verb: 'DELETE', path: '/projects/{project_id}', kind: 'unary' },
  'project.restore': { verb: 'POST', path: '/projects/actions/restore', kind: 'unary' },
  'project.pinned_sessions': { verb: 'GET', path: '/projects/pinned-sessions', kind: 'unary' },
};

const PATH_PLACEHOLDER = /\{([A-Za-z_][A-Za-z0-9_]*)\}/g;

export function lookupWebRestRoute(method: string): RouteRow | null {
  return ROUTES[method] ?? null;
}

function joinGatewayUrl(baseUrl: string, path: string): string {
  const base = baseUrl.replace(/\/+$/, '');
  const rel = path.startsWith('/') ? path : `/${path}`;
  return `${base}${rel}`;
}

function fillPath(
  template: string,
  params: Record<string, unknown>
): { path: string; used: Set<string> } {
  const used = new Set<string>();
  const missing: string[] = [];
  const path = template.replace(PATH_PLACEHOLDER, (_all, key: string) => {
    const value = params[key];
    if (value === undefined || value === null || String(value) === '') {
      missing.push(key);
      return _all;
    }
    used.add(key);
    return encodeURIComponent(String(value));
  });
  if (missing.length > 0) {
    throw new RestAssemblyError(`REST 路径缺占位符 ${missing.join(',')}: ${template}`, missing);
  }
  return { path, used };
}

function remainingParams(
  params: Record<string, unknown>,
  used: Set<string>
): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [key, value] of Object.entries(params)) {
    if (used.has(key) || value === undefined) {
      continue;
    }
    out[key] = value;
  }
  return out;
}

function queryValues(remaining: Record<string, unknown>): Record<string, string> | null {
  const query: Record<string, string> = {};
  for (const [key, value] of Object.entries(remaining)) {
    if (value === null) {
      continue;
    }
    if (typeof value === 'object') {
      query[key] = JSON.stringify(value);
    } else {
      query[key] = String(value);
    }
  }
  return Object.keys(query).length > 0 ? query : null;
}

function applyMethodBody(
  method: string,
  remaining: Record<string, unknown>
): Record<string, unknown> {
  if (method === 'chat.send') {
    const body: Record<string, unknown> = { ...remaining, enable_streaming: true };
    if (body.query == null && body.content != null) {
      body.query = body.content;
    } else if (body.content == null && body.query != null) {
      body.content = body.query;
    }
    return body;
  }
  return remaining;
}

export function assembleWebRest(
  method: string,
  params: Record<string, unknown> | undefined,
  baseUrl: string
): AssembledWebRest | null {
  const name = method.trim();
  if (!name) {
    throw new RestAssemblyError('method 为空，无法组装 REST');
  }
  const row = lookupWebRestRoute(name);
  if (!row) {
    return null;
  }
  const values = { ...(params ?? {}) };
  const { path, used } = fillPath(row.path, values);
  const remaining = applyMethodBody(name, remainingParams(values, used));
  const useQuery = row.verb === 'GET' || row.kind === 'history-stream';
  return {
    method: name,
    verb: row.verb,
    url: joinGatewayUrl(baseUrl, path),
    query: useQuery ? queryValues(remaining) : null,
    jsonBody: useQuery ? null : remaining,
    kind: row.kind,
  };
}

function appendQuery(url: string, query: Record<string, string> | null): string {
  if (!query) {
    return url;
  }
  const encoded = Object.entries(query)
    .map(([key, value]) => `${encodeURIComponent(key)}=${encodeURIComponent(value)}`)
    .join('&');
  return encoded ? `${url}?${encoded}` : url;
}

interface UnaryOk {
  ok: true;
  payload: unknown;
  requestId?: string;
}

interface UnaryFail {
  ok: false;
  message: string;
  code?: string;
  requestId?: string;
}

type UnaryResult = UnaryOk | UnaryFail;

interface SseFrame {
  event?: string;
  data?: string;
  id?: string;
}

function errorFields(error: unknown): { message: string; code?: string } {
  if (typeof error === 'string' && error.trim()) {
    return { message: error };
  }
  if (isRecord(error)) {
    const message =
      typeof error.message === 'string' && error.message.trim()
        ? error.message
        : typeof error.error === 'string'
          ? error.error
          : 'request failed';
    const code = typeof error.code === 'string' ? error.code : undefined;
    return { message, code };
  }
  return { message: 'request failed' };
}

export function unwrapHttpUnary(input: unknown, status?: number): UnaryResult {
  if (!isRecord(input)) {
    return { ok: false, message: 'empty http response', code: 'HTTP_ERROR' };
  }
  const requestId = typeof input.request_id === 'string' ? input.request_id : undefined;
  if (typeof input.ok === 'boolean') {
    if (input.ok) {
      const payload = input.data !== undefined ? input.data : input.payload;
      return { ok: true, payload: payload ?? {}, requestId };
    }
    const { message, code } = errorFields(input.error);
    return { ok: false, message, code, requestId };
  }
  if (input.agent_ready !== undefined) {
    return { ok: true, payload: input, requestId };
  }
  // 鉴权代理层（manager-web 同源反代等）返回 FastAPI/Starlette 的 {"detail": ...}
  // 错误形状——不是标准信封，但要给出真实原因而非笼统的 "invalid http envelope"。
  if (input.detail !== undefined) {
    const detail = input.detail;
    const message =
      typeof detail === 'string' && detail.trim()
        ? detail
        : 'request rejected';
    const code =
      status === 401 ? 'UNAUTHORIZED' : status === 403 ? 'FORBIDDEN' : 'HTTP_ERROR';
    return { ok: false, message, code, requestId };
  }
  return { ok: false, message: 'invalid http envelope', code: 'HTTP_ERROR', requestId };
}

function parseSseBlock(block: string): SseFrame | null {
  const trimmed = block.replace(/^\uFEFF/, '').trimEnd();
  if (!trimmed) {
    return null;
  }
  const frame: SseFrame = {};
  for (const rawLine of trimmed.split(/\r?\n/)) {
    if (!rawLine || rawLine.startsWith(':')) {
      continue;
    }
    const idx = rawLine.indexOf(':');
    const field = idx === -1 ? rawLine : rawLine.slice(0, idx);
    let value = idx === -1 ? '' : rawLine.slice(idx + 1);
    if (value.startsWith(' ')) {
      value = value.slice(1);
    }
    if (field === 'event') {
      frame.event = value;
    } else if (field === 'data') {
      frame.data = frame.data === undefined ? value : `${frame.data}\n${value}`;
    } else if (field === 'id') {
      frame.id = value;
    }
  }
  if (frame.event === undefined && frame.data === undefined && frame.id === undefined) {
    return null;
  }
  return frame;
}

export function consumeSseBuffer(buffer: string): { frames: SseFrame[]; rest: string } {
  const parts = buffer.split(/\r?\n\r?\n/);
  const rest = parts.pop() ?? '';
  const frames: SseFrame[] = [];
  for (const part of parts) {
    const frame = parseSseBlock(part);
    if (frame) {
      frames.push(frame);
    }
  }
  return { frames, rest };
}

function payloadFromData(
  data: string | undefined
): { payload: Record<string, unknown>; raw: Record<string, unknown> | null } {
  if (!data) {
    return { payload: {}, raw: null };
  }
  try {
    const parsed: unknown = JSON.parse(data);
    if (isRecord(parsed)) {
      if (isRecord(parsed.payload)) {
        return { payload: parsed.payload, raw: parsed };
      }
      return { payload: parsed, raw: parsed };
    }
    return { payload: { value: parsed }, raw: null };
  } catch {
    return { payload: { raw: data }, raw: null };
  }
}

function extractSseRequestId(
  raw: Record<string, unknown> | null,
  payload: Record<string, unknown>,
  frameId?: string
): string | undefined {
  if (raw) {
    const top = typeof raw.request_id === 'string' ? raw.request_id : undefined;
    if (top) return top;
  }
  const inner = typeof payload.request_id === 'string' ? payload.request_id : undefined;
  if (inner) return inner;
  const rid = typeof payload.rid === 'string' ? payload.rid : undefined;
  if (rid) return rid;
  const id = typeof frameId === 'string' ? frameId.trim() : '';
  return id || undefined;
}

export function sseFrameToWsEvent(frame: SseFrame): WsEvent | null {
  let eventName = frame.event?.trim() ?? '';
  const { payload, raw } = payloadFromData(frame.data);
  if (!eventName && typeof payload.event === 'string') {
    eventName = payload.event;
  }
  if (!eventName) {
    return null;
  }
  // SSE 标准 id 字段承载 Gateway 生成的 request_id；HTTP 模式下 data
  // 往往不重复携带该字段，必须保留它才能恢复 WS 帧的请求关联语义。
  const requestId = extractSseRequestId(
    raw,
    payload,
    isEnterprise() ? frame.id : undefined
  );
  return {
    type: 'event',
    event: eventName,
    payload,
    ...(requestId ? { request_id: requestId } : {}),
  };
}

export function historyPageToEvents(page: unknown, sessionId?: string): WsEvent[] {
  if (!isRecord(page)) {
    return [];
  }
  const sid =
    (typeof page.session_id === 'string' && page.session_id) || sessionId || '';
  const events: WsEvent[] = [];
  const messages = Array.isArray(page.messages) ? page.messages : [];
  for (const message of messages) {
    events.push({
      type: 'event',
      event: 'history.message',
      payload: {
        session_id: sid,
        page_idx: page.page_idx,
        total_pages: page.total_pages,
        message,
      },
    });
  }
  events.push({
    type: 'event',
    event: 'history.message',
    payload: {
      session_id: sid,
      page_idx: page.page_idx,
      total_pages: page.total_pages,
      status: 'done',
    },
  });
  return events;
}

function isSseContentType(contentType: string | null | undefined): boolean {
  return Boolean(contentType && contentType.toLowerCase().includes('text/event-stream'));
}

function isChatSseTerminal(event: WsEvent): boolean {
  if (event.event === 'chat.error') {
    return true;
  }
  // 仅企业版使用任务级 SSE 生命周期：chat.final 只是回复段结束，
  // 必须继续读到 processing_status(false)，避免工具状态停在 pending。
  // 个人版保持原有 chat.final 即结束的协议，避免改变个人版行为。
  if (!isEnterprise()) {
    return event.event === 'chat.final';
  }
  return (
    event.event === 'chat.processing_status' &&
    isRecord(event.payload) &&
    event.payload.is_processing === false
  );
}

/**
 * HTTP unary ``chat.interrupt`` 把 ``accepted`` 与 ``interrupt_result`` 合在同一 JSON body。
 * 只在 Gateway 明确给出 ``event_type`` 时映射为 WS 事件，前端不伪造 success。
 */
export function interruptUnaryToEvents(payload: unknown, requestId?: string): WsEvent | null {
  if (!isRecord(payload) || payload.event_type !== 'chat.interrupt_result') {
    return null;
  }
  return {
    type: 'event',
    event: 'chat.interrupt_result',
    // HTTP 合成事件补上 interrupt 请求 id：interrupt_result 处理器按
    // event.request_id 清理 pendingInterruptRequestIdsRef，缺失会缓慢泄漏。
    ...(requestId ? { request_id: requestId } : {}),
    payload: { ...payload },
  };
}

function interruptIntentOf(payload: Record<string, unknown>): string {
  return typeof payload.intent === 'string' && payload.intent.trim()
    ? payload.intent.trim()
    : 'cancel';
}

function isHistorySseDone(payload: Record<string, unknown>): boolean {
  return typeof payload.status === 'string' && payload.status.trim().toLowerCase() === 'done';
}

export class WebHttpClient {
  private state: WebConnectionState = 'idle';
  private handlers = new Map<string, Set<EventHandler>>();
  private stateHandlers = new Set<StateHandler>();
  private inflight = new Map<string, AbortController>();
  /** 仍在泵的 ``chat.send`` SSE，按 session 隔离；pause/resume 不碰。 */
  private sseInflight = new Map<string, { controller: AbortController; sessionId: string }>();
  /** 被后续 chat.send 顶掉的 SSE，abort 后不得再发 interrupt / 不得当发送失败。 */
  private sseSuperseded = new Set<string>();
  private connectAbort: AbortController | null = null;
  private reconnectTimer: number | null = null;
  private reconnectAttempts = 0;
  private manualClose = false;
  private connectPromise: Promise<void> | null = null;
  private lastConnectOptions: WebConnectOptions = {};
  private requestSeq = 0;
  private streamEventFilter: ((event: WsEvent) => boolean) | null = null;
  private pauseBufferHook: PauseBufferHook | null = null;

  getState(): WebConnectionState {
    return this.state;
  }

  getInflightCount(): number {
    return this.inflight.size;
  }

  onStateChange(handler: StateHandler): () => void {
    this.stateHandlers.add(handler);
    return () => {
      this.stateHandlers.delete(handler);
    };
  }

  /** 丢弃已 cancel 的 chat/context 流式事件（在 dispatch 前统一过滤） */
  setStreamEventFilter(filter: ((event: WsEvent) => boolean) | null): void {
    this.streamEventFilter = filter;
  }

  /** 暂停期间将流式输出写入暂存区，恢复后通过 replayBufferedEvent 回放 */
  setPauseBufferHook(hook: PauseBufferHook | null): void {
    this.pauseBufferHook = hook;
  }

  /** 回放暂存的事件——直接派发给 handler，绕过 filter 和 buffer */
  replayBufferedEvent(event: WsEvent): void {
    this.dispatchEventToHandlers(event);
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

  async connect(options: WebConnectOptions = {}): Promise<void> {
    if (this.state === 'ready') {
      return;
    }
    if (this.connectPromise) {
      return this.connectPromise;
    }

    this.lastConnectOptions = options;
    this.manualClose = false;
    this.updateState(this.reconnectAttempts > 0 ? 'reconnecting' : 'connecting');

    this.connectPromise = this.openConnection();
    return this.connectPromise;
  }

  disconnect(_reason = 'User disconnect'): Promise<void> {
    this.manualClose = true;
    this.clearReconnectTimer();
    this.connectAbort?.abort();
    this.connectAbort = null;
    this.abortInflight();
    this.connectPromise = null;
    this.updateState('closed');
    return Promise.resolve();
  }

  async request<T = unknown>(
    method: string,
    params?: Record<string, unknown>,
    options: WebRequestOptions = {}
  ): Promise<T> {
    await this.ensureReady();
    if (this.state !== 'ready') {
      throw this.createWebError(
        i18n.t('network.connectionUnavailable'),
        'WS_NOT_READY',
        undefined,
        true
      );
    }

    const requestId = options.requestId ?? this.generateRequestId();
    let assembled;
    try {
      assembled = assembleWebRest(method, params, getGatewayHttpBase());
    } catch (error) {
      if (error instanceof RestAssemblyError) {
        throw this.createWebError(error.message, 'BAD_REQUEST', requestId, false);
      }
      throw error;
    }
    if (!assembled) {
      throw this.createWebError(
        i18n.t('network.requestFailed'),
        'METHOD_NOT_FOUND',
        requestId,
        false
      );
    }

    const controller = new AbortController();
    this.inflight.set(requestId, controller);
    const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
    const timeoutId = window.setTimeout(() => {
      controller.abort();
    }, timeoutMs);

    const onAbort = () => {
      controller.abort();
    };
    if (options.signal) {
      if (options.signal.aborted) {
        window.clearTimeout(timeoutId);
        this.inflight.delete(requestId);
        throw this.createWebError(
          i18n.t('network.requestAborted'),
          'REQUEST_ABORTED',
          requestId,
          false
        );
      }
      options.signal.addEventListener('abort', onAbort, { once: true });
    }

    try {
      const chatSessionId = pickHeaderString(params?.session_id);
      if (assembled.kind === 'sse') {
        this.abortSseInflight({ exceptRequestId: requestId, sessionId: chatSessionId });
        this.sseInflight.set(requestId, {
          controller,
          sessionId: chatSessionId ?? '',
        });
      }
      const headers: Record<string, string> = {
        ...this.identityHeaders(requestId, params ?? {}),
        Accept: assembled.kind === 'unary' ? 'application/json' : 'text/event-stream',
      };
      if (assembled.jsonBody) {
        headers['Content-Type'] = 'application/json';
      }
      const response = await this.authenticatedFetch(appendQuery(assembled.url, assembled.query), {
        method: assembled.verb,
        headers,
        body: assembled.jsonBody ? JSON.stringify(assembled.jsonBody) : undefined,
        signal: controller.signal,
      });
      window.clearTimeout(timeoutId);

      if (assembled.kind === 'sse' || assembled.kind === 'history-stream') {
        if (isSseContentType(response.headers.get('content-type'))) {
          if (!response.ok) {
            const unwrapped = unwrapHttpUnary(await this.readJson(response), response.status);
            throw this.createWebError(
              unwrapped.ok ? i18n.t('network.requestFailed') : unwrapped.message,
              unwrapped.ok ? 'HTTP_ERROR' : unwrapped.code,
              requestId,
              response.status >= 500
            );
          }
          const accepted = {
            accepted: true,
            session_id: typeof params?.session_id === 'string' ? params.session_id : undefined,
            ...(assembled.kind === 'history-stream' && params?.page_idx !== undefined
              ? { page_idx: params.page_idx }
              : {}),
          };
          void this.pumpSse(
            response,
            requestId,
            assembled.kind,
            controller,
            chatSessionId
          );
          return accepted as T;
        }
        if (assembled.kind === 'history-stream') {
          const page = await this.readUnaryPayload(response, requestId);
          const sessionId = typeof params?.session_id === 'string' ? params.session_id : undefined;
          for (const event of historyPageToEvents(page, sessionId)) {
            this.dispatchEvent(event);
          }
          this.inflight.delete(requestId);
          return page as T;
        }
      }

      const payload = await this.readUnaryPayload(response, requestId);
      this.inflight.delete(requestId);
      if (method === 'chat.interrupt') {
        const event = interruptUnaryToEvents(payload, requestId);
        if (event) {
          this.dispatchEvent(event);
          if (event.payload.success === true && interruptIntentOf(event.payload) === 'cancel') {
            this.abortSseInflight({
              sessionId:
                pickHeaderString(event.payload.session_id) ??
                pickHeaderString(params?.session_id),
            });
          }
        }
      }
      return payload as T;
    } catch (error) {
      window.clearTimeout(timeoutId);
      this.inflight.delete(requestId);
      this.sseInflight.delete(requestId);
      if (this.sseSuperseded.delete(requestId)) {
        // 新 chat.send 只中止浏览器读旧 SSE，Agent 侧由 Gateway new_chat_send 取消。
        return {
          accepted: true,
          session_id: typeof params?.session_id === 'string' ? params.session_id : undefined,
        } as T;
      }
      if (options.signal?.aborted) {
        if (assembled.kind === 'sse' && typeof params?.session_id === 'string') {
          void this.interruptChat(params.session_id, requestId);
        }
        throw this.createWebError(
          i18n.t('network.requestAborted'),
          'REQUEST_ABORTED',
          requestId,
          false
        );
      }
      if (error instanceof DOMException && error.name === 'AbortError') {
        if (assembled.kind === 'sse' && typeof params?.session_id === 'string') {
          void this.interruptChat(params.session_id, requestId);
        }
        throw this.createWebError(
          i18n.t('network.requestTimeout'),
          'REQUEST_TIMEOUT',
          requestId,
          true
        );
      }
      throw this.asWebError(error, requestId, true);
    } finally {
      options.signal?.removeEventListener('abort', onAbort);
    }
  }

  async sendFireAndForget(
    _method: string,
    _params?: Record<string, unknown>,
    _options: { isStream?: boolean } = {}
  ): Promise<void> {
    throw this.createWebError(
      i18n.t('network.requestFailed'),
      'METHOD_NOT_FOUND',
      undefined,
      false
    );
  }

  private async ensureReady(): Promise<void> {
    if (this.state === 'ready') {
      return;
    }
    await this.connect(this.lastConnectOptions);
  }

  private async openConnection(): Promise<void> {
    const requestId = this.generateRequestId();
    this.connectAbort = new AbortController();
    const url = `${getGatewayHttpBase().replace(/\/+$/, '')}/connection/status`;
    try {
      const response = await this.authenticatedFetch(url, {
        method: 'GET',
        headers: this.identityHeaders(requestId, {}),
        signal: this.connectAbort.signal,
      });
      const unwrapped = unwrapHttpUnary(await this.readJson(response), response.status);
      if (!unwrapped.ok) {
        throw this.createWebError(
          unwrapped.message,
          unwrapped.code ?? (response.status >= 500 ? 'HTTP_ERROR' : 'WS_ERROR'),
          unwrapped.requestId ?? requestId,
          response.status >= 500 || !response.ok
        );
      }
      this.reconnectAttempts = 0;
      this.updateState('ready');
      this.dispatchEvent({
        type: 'event',
        event: 'connection.ack',
        payload: isRecord(unwrapped.payload) ? { ...unwrapped.payload } : {},
      });
    } catch (error) {
      if (this.state !== 'ready') {
        this.updateState('closed');
      }
      if (!this.manualClose) {
        this.scheduleReconnect();
      }
      throw this.asWebError(error, requestId, true);
    } finally {
      this.connectAbort = null;
      this.connectPromise = null;
    }
  }

  private async pumpSse(
    response: Response,
    requestId: string,
    kind: 'sse' | 'history-stream',
    controller: AbortController,
    sessionId?: string
  ): Promise<void> {
    const reader = response.body?.getReader();
    if (!reader) {
      this.inflight.delete(requestId);
      this.sseInflight.delete(requestId);
      return;
    }
    if (kind === 'sse') {
      this.sseInflight.set(requestId, {
        controller,
        sessionId: sessionId ?? this.sseInflight.get(requestId)?.sessionId ?? '',
      });
    }
    const decoder = new TextDecoder();
    let buffer = '';
    try {
      while (!controller.signal.aborted) {
        const { done, value } = await reader.read();
        if (done) {
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        const consumed = consumeSseBuffer(buffer);
        buffer = consumed.rest;
        for (const frame of consumed.frames) {
          const event = sseFrameToWsEvent(frame);
          if (!event) {
            continue;
          }
          this.dispatchEvent(event);
          if (kind === 'sse' && isChatSseTerminal(event)) {
            await reader.cancel().catch(() => undefined);
            return;
          }
          if (kind === 'history-stream' && isHistorySseDone(event.payload)) {
            await reader.cancel().catch(() => undefined);
            return;
          }
        }
      }
      if (buffer.trim()) {
        for (const frame of consumeSseBuffer(`${buffer}\n\n`).frames) {
          const event = sseFrameToWsEvent(frame);
          if (event) {
            this.dispatchEvent(event);
          }
        }
      }
    } catch {
      // abort / 断流：hook 靠现有 on(chat.error) 或 inflight 归零
    } finally {
      this.inflight.delete(requestId);
      this.sseInflight.delete(requestId);
      this.sseSuperseded.delete(requestId);
    }
  }

  private async interruptChat(sessionId: string, requestId: string): Promise<void> {
    try {
      const assembled = assembleWebRest(
        'chat.interrupt',
        { session_id: sessionId, intent: 'cancel' },
        getGatewayHttpBase()
      );
      if (!assembled) {
        return;
      }
      await this.authenticatedFetch(appendQuery(assembled.url, assembled.query), {
        method: assembled.verb,
        headers: {
          ...this.identityHeaders(requestId, { session_id: sessionId }),
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(assembled.jsonBody ?? { intent: 'cancel' }),
      });
    } catch {
      // 打断失败不掩盖本次 abort
    }
  }

  private async readUnaryPayload(response: Response, requestId: string): Promise<unknown> {
    const unwrapped = unwrapHttpUnary(await this.readJson(response), response.status);
    if (!unwrapped.ok) {
      throw this.createWebError(
        unwrapped.message,
        unwrapped.code ?? (response.status >= 500 ? 'HTTP_ERROR' : undefined),
        unwrapped.requestId ?? requestId,
        this.isRetriableCode(unwrapped.code) || response.status >= 500
      );
    }
    if (!response.ok && response.status >= 400 && response.status !== 201) {
      throw this.createWebError(
        i18n.t('network.requestFailed'),
        response.status >= 500 ? 'HTTP_ERROR' : 'BAD_REQUEST',
        requestId,
        response.status >= 500
      );
    }
    return unwrapped.payload;
  }

  private async readJson(response: Response): Promise<unknown> {
    const text = await response.text();
    if (!text.trim()) {
      return {};
    }
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return { ok: false, error: { code: 'HTTP_ERROR', message: text.slice(0, 200) } };
    }
  }

  private identityHeaders(
    requestId: string,
    params: Record<string, unknown>
  ): Record<string, string> {
    return buildRuntimeIdentityHeaders(requestId, params);
  }

  private authenticatedFetch(input: RequestInfo | URL, init: RequestInit): Promise<Response> {
    if (isEnterprise() && hasManagerSessionCredentials()) {
      return managerAuthenticatedFetch(input, init);
    }
    return fetch(input, init);
  }

  private dispatchEvent(event: WsEvent): void {
    if (this.streamEventFilter && !this.streamEventFilter(event)) {
      return;
    }
    const hook = this.pauseBufferHook;
    if (hook?.isActive()) {
      if (event.event === 'chat.processing_status') {
        return;
      }
      if (PAUSABLE_STREAM_EVENTS.has(event.event)) {
        hook.onBuffer(event);
        return;
      }
    }
    this.dispatchEventToHandlers(event);
  }

  private dispatchEventToHandlers(event: WsEvent): void {
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
    const delay =
      this.reconnectAttempts <= MAX_RECONNECT_ATTEMPTS
        ? Math.min(1000 * 2 ** (this.reconnectAttempts - 1), 30000)
        : 2000;
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

  private abortInflight(): void {
    this.inflight.forEach((controller) => {
      controller.abort();
    });
    this.inflight.clear();
    this.sseInflight.clear();
    this.sseSuperseded.clear();
  }

  private abortSseInflight(opts: { exceptRequestId?: string; sessionId?: string } = {}): void {
    const { exceptRequestId, sessionId } = opts;
    if (!sessionId) {
      return;
    }
    const toAbort: string[] = [];
    this.sseInflight.forEach((entry, id) => {
      if (id === exceptRequestId || entry.sessionId !== sessionId) {
        return;
      }
      toAbort.push(id);
    });
    for (const id of toAbort) {
      const entry = this.sseInflight.get(id);
      this.sseInflight.delete(id);
      this.sseSuperseded.add(id);
      entry?.controller.abort();
    }
  }

  private updateState(state: WebConnectionState): void {
    this.state = state;
    this.stateHandlers.forEach((handler) => {
      handler(state);
    });
  }

  private generateRequestId(): string {
    this.requestSeq += 1;
    return `req_${Date.now().toString(36)}_${this.requestSeq}`;
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
      code === 'WS_NOT_READY' ||
      code === 'HTTP_ERROR' ||
      code === 'SERVICE_UNAVAILABLE'
    );
  }

  private asWebError(error: unknown, requestId?: string, retriable = false): WebError {
    if (error instanceof Error && 'code' in error) {
      return error as WebError;
    }
    const message = error instanceof Error ? error.message : i18n.t('network.requestFailed');
    return this.createWebError(message, 'HTTP_ERROR', requestId, retriable);
  }
}
