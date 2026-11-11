export type JsonObject = Record<string, unknown>;

export interface GatewayEvent { event: string; payload: JsonObject }

/** A refused or failed request; ``code`` is the refusal's code, absent when the request never got an answer. */
export class GatewayRequestError extends Error {
  constructor(message: string, readonly code?: string) {
    super(message);
    this.name = 'GatewayRequestError';
  }
}

/** Events that name no conversation and still concern the pane. */
const SESSIONLESS_EVENTS = new Set(['models.updated']);

/** Whether an event is for the pane showing conversation ``sessionId``. */
export function acceptsSessionEvent(event: GatewayEvent, sessionId: string): boolean {
  const owner = event.payload.session_id;
  return owner ? owner === sessionId : SESSIONLESS_EVENTS.has(event.event);
}

interface PendingRequest {
  resolve: (value: JsonObject) => void;
  reject: (reason: GatewayRequestError) => void;
  timer: ReturnType<typeof setTimeout>;
}

/** Whether the socket is open; while it is down, the attempts so far and when the next one starts. */
export type ConnectionState = { kind: 'open' } | { kind: 'down'; attempt: number; retryAt: number };

/** The pane's WebSocket to the Gateway: requests, events, and reconnecting after a drop. */
export class GatewayClient {
  private socket: WebSocket | null = null;
  private connectPromise: Promise<void> | null = null;
  private sequence = 0;
  private readonly pending = new Map<string, PendingRequest>();
  private readonly listeners = new Set<(event: GatewayEvent) => void>();
  private readonly reconnectListeners = new Set<() => void>();
  private readonly connectionListeners = new Set<(state: ConnectionState) => void>();
  private reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  private reconnectAttempt = 0;
  private everConnected = false;
  private stopped = false;

  constructor(private readonly appId: string) {
    window.addEventListener('beforeunload', () => this.disconnect(), { once: true });
  }

  connect(): Promise<void> {
    if (this.stopped) return Promise.reject(new GatewayRequestError('Gateway client has stopped'));
    if (this.socket?.readyState === WebSocket.OPEN) return Promise.resolve();
    if (this.connectPromise) return this.connectPromise;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = undefined;
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const query = new URLSearchParams({ app_id: this.appId, user_id: getOrCreateOfficeUserId(this.appId) });
    const socket = new WebSocket(`${protocol}//${window.location.host}/ws?${query}`);
    this.socket = socket;
    const opening = new Promise<void>((resolve, reject) => {
      socket.addEventListener('message', event => this.handleMessage(String(event.data)));
      socket.addEventListener('open', () => {
        const reconnected = this.everConnected || this.reconnectAttempt > 0;
        this.everConnected = true;
        this.reconnectAttempt = 0;
        resolve();
        if (reconnected) {
          this.connectionListeners.forEach(listener => listener({ kind: 'open' }));
          this.reconnectListeners.forEach(listener => listener());
        }
      }, { once: true });
      socket.addEventListener('error', () => {
        reject(new GatewayRequestError('无法连接 WorkSwarm'));
        socket.close();
      }, { once: true });
      socket.addEventListener('close', () => {
        reject(new GatewayRequestError('Gateway connection closed'));
        if (this.socket !== socket) return;
        this.socket = null;
        this.rejectPending('Gateway connection closed');
        this.scheduleReconnect();
      }, { once: true });
    });
    const shared = opening.finally(() => {
      if (this.connectPromise === shared) this.connectPromise = null;
    });
    this.connectPromise = shared;
    return shared;
  }

  onEvent(listener: (event: GatewayEvent) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  /** Called after a dropped connection is open again, including when the very first attempt had failed. */
  onReconnect(listener: () => void): () => void {
    this.reconnectListeners.add(listener);
    return () => this.reconnectListeners.delete(listener);
  }

  /** Called when the socket goes down or a reconnect is scheduled, and when it is open again. */
  onConnectionChange(listener: (state: ConnectionState) => void): () => void {
    this.connectionListeners.add(listener);
    return () => this.connectionListeners.delete(listener);
  }

  disconnect(): void {
    this.stopped = true;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = undefined;
    this.socket?.close();
    this.rejectPending('Gateway client stopped');
  }

  private scheduleReconnect(): void {
    if (this.stopped || this.reconnectTimer !== undefined) return;
    const delay = Math.min(30_000, 1000 * 2 ** Math.min(this.reconnectAttempt++, 5));
    const down: ConnectionState = { kind: 'down', attempt: this.reconnectAttempt, retryAt: Date.now() + delay };
    this.connectionListeners.forEach(listener => listener(down));
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = undefined;
      this.connect().catch(() => this.scheduleReconnect());
    }, delay);
  }

  /** The answer's payload; a refusal or a lost connection rejects with a ``GatewayRequestError``. */
  async request(
    method: string, params: JsonObject = {}, options: { stream?: boolean; timeoutMs?: number } = {},
  ): Promise<JsonObject> {
    await this.connect();
    const socket = this.socket;
    if (socket?.readyState !== WebSocket.OPEN) throw new GatewayRequestError('Gateway connection is not open');
    const id = `office_${Date.now().toString(36)}_${++this.sequence}`;
    const answered = new Promise<JsonObject>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new GatewayRequestError(`${method} 请求超时`));
      }, options.timeoutMs ?? 30_000);
      this.pending.set(id, { resolve, reject, timer });
    });
    socket.send(JSON.stringify({ type: 'req', id, method, params, ...(options.stream ? { is_stream: true } : {}) }));
    return answered;
  }

  private handleMessage(raw: string): void {
    let message: unknown;
    try {
      message = JSON.parse(raw);
    } catch {
      return;
    }
    if (!isRecord(message)) return;
    const payload = isRecord(message.payload) ? message.payload : {};
    if (message.type === 'res' && typeof message.id === 'string') {
      const pending = this.pending.get(message.id);
      if (!pending) return;
      clearTimeout(pending.timer);
      this.pending.delete(message.id);
      if (message.ok === true) pending.resolve(payload);
      // The Gateway lifts a refusal's code to the frame, also for the ones AgentServer answers.
      else pending.reject(new GatewayRequestError(
        typeof message.error === 'string' ? message.error : 'Gateway request failed',
        typeof message.code === 'string' ? message.code : undefined));
      return;
    }
    if (message.type === 'event' && typeof message.event === 'string') {
      const event: GatewayEvent = { event: message.event, payload };
      this.listeners.forEach(listener => listener(event));
    }
  }

  private rejectPending(message: string): void {
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new GatewayRequestError(message));
    }
    this.pending.clear();
  }
}

function getOrCreateOfficeUserId(appId: string): string {
  const storageKey = `jiuwenswarm-office-user-id:${appId}`;
  const existing = window.localStorage.getItem(storageKey);
  if (existing) return existing;
  const created = `office-${crypto.randomUUID()}`;
  window.localStorage.setItem(storageKey, created);
  return created;
}

export function isRecord(value: unknown): value is JsonObject {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
