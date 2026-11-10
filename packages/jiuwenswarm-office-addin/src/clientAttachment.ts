/**
 * The pane's attachment of its resource (an open document) to one conversation,
 * and the tool calls that conversation's paused turns hand it.
 *
 * The backend's attachment directory owns who is attached: a lease the pane
 * renews every ``renew_every_seconds`` of the attach response. The pane only
 * holds the handle. A renew refused with ``NOT_ATTACHED`` makes it attach
 * again; after a reconnect it attaches right away, as the new socket has no
 * attachment to renew. An attach again that fails keeps the attachment it had
 * and its renews.
 *
 * A paused turn streams one ``chat.client_tool_request`` per call it waits on,
 * and every attach answers with the calls and questions the pause still waits
 * on (``pending``); both arrive here as the same events. The pane runs each
 * call once and answers the finished calls of the conversation it shows in one
 * ``chat.send`` while that conversation's turn is not running, as the backend
 * takes answers only for calls it is paused on. The answers of a conversation
 * the pane left wait until it attaches that conversation again.
 */
import {
  acceptsSessionEvent, GatewayRequestError, isRecord,
  type GatewayClient, type GatewayEvent, type JsonObject,
} from './gatewayClient';
import { workerTimer, type Timer } from './workerTimer';

export type SessionId = string & { readonly __brand: 'SessionId' };

/** What the pane attaches besides the conversation; the app is the conversation's ``client_app``. */
export interface AttachDeclaration {
  resource: { key: string; display_name: string; kind: string; host_vendor: string };
  capabilities: JsonObject;
}

/** One call of a ``chat.client_tool_request``. */
export interface ClientToolRequest {
  /** The tool call id, answered as is; a call run through ``tool_call`` has the id ``<id>:target``. */
  requestId: string;
  interruptId: string;
  toolName: string;
  input: JsonObject;
  /** Signed download links of the assets the call's input names, by asset handle. */
  downloads: Readonly<Record<string, string>>;
}

export type ContentBlock =
  | { type: 'text'; text: string }
  | { type: 'image'; source: { type: 'base64'; media_type: 'image/png'; data: string } };

/** A call's result as agent-core's ``ClientToolResult`` reads it; any other key would make it one JSON value. */
export interface ClientToolResult {
  status: 'completed' | 'error' | 'cancelled' | 'timeout';
  content: ContentBlock[];
  is_error: boolean;
  data: JsonObject;
}

/** Runs the calls; never rejects. */
export interface ClientToolExecutor {
  execute(request: ClientToolRequest): Promise<ClientToolResult>;
  /** The answer to a call while another window holds the resource; nothing runs. */
  declineHeldElsewhere(request: ClientToolRequest): ClientToolResult;
  /** Stop what has not started yet, as when the user cancels the turn. */
  cancelAll(): void;
}

export type AttachmentState =
  | { kind: 'detached' }
  | {
    kind: 'attached'; sessionId: SessionId; attachmentId: string; displayName: string; renewEverySeconds: number;
  }
  /**
   * Another window attached the same document; this one stopped renewing and leaves it there until ``attach``.
   * It answers the calls its conversation still sends it as run in that window, so its turn does not wait on them.
   */
  | { kind: 'displaced'; sessionId: SessionId }
  /**
   * Shown to the user. A lost request or ``UNAVAILABLE`` tries again every ``RETRY_SECONDS``; any failure tries
   * again on the next reconnect or ``attach``. The calls that arrive meanwhile wait for the attach that hands them
   * back, or for the backend to answer them as gone.
   */
  | { kind: 'failed'; sessionId: SessionId | undefined; error: unknown };

/** A call of one conversation, from its start until its answer is taken. */
type Call = { sessionId: SessionId } & (
  | { kind: 'running' }
  | { kind: 'done'; answer: ClientToolAnswer }
  | { kind: 'sending'; answer: ClientToolAnswer });

interface ClientToolAnswer { request_id: string; interrupt_id: string; result: ClientToolResult }

const NEW_CONVERSATION_CODES = new Set(['CONFLICT', 'NOT_FOUND_OR_FORBIDDEN']);
const RENAME_POLL_MS = 5_000;
/** How often a failed attach tries again, as often as a renew. */
const RETRY_SECONDS = 30;

export class ClientAttachment {
  private state: AttachmentState = { kind: 'detached' };
  private readonly timer: Timer;
  /** Cancels the next renew, or the next try of a failed attach. */
  private cancelTimer = (): void => undefined;
  private readonly calls = new Map<string, Call>();
  /** The conversation whose turn the pane last saw running; its answers wait until the turn pauses. */
  private runningTurn: SessionId | undefined;
  private readonly eventListeners = new Set<(event: GatewayEvent) => void>();
  private readonly stateListeners = new Set<(state: AttachmentState) => void>();

  constructor(private readonly options: {
    gateway: Pick<GatewayClient, 'request' | 'onEvent' | 'onReconnect'>;
    executor: ClientToolExecutor;
    /** Read on every attach, so a renamed file or a changed host is attached as it is now. */
    declaration: () => Promise<AttachDeclaration>;
    createSession: () => Promise<SessionId>;
    /**
     * Times the renews, which a hidden pane must still send before the lease lapses, and the retries of a failed
     * attach; ``workerTimer`` by default.
     */
    renewTimer?: Timer;
  }) {
    this.timer = options.renewTimer ?? workerTimer();
    options.gateway.onEvent(event => {
      const sessionId = this.sessionId();
      if (sessionId && acceptsSessionEvent(event, sessionId)) this.deliver(event, sessionId);
    });
    options.gateway.onReconnect(() => {
      const { state } = this;
      if (state.kind === 'attached' || state.kind === 'failed') void this.open(state.sessionId);
    });
  }

  current(): AttachmentState {
    return this.state;
  }

  sessionId(): SessionId | undefined {
    return this.state.kind === 'detached' ? undefined : this.state.sessionId;
  }

  /** The events of the attached conversation, including what an attach hands back as pending. */
  onEvent(listener: (event: GatewayEvent) => void): () => void {
    this.eventListeners.add(listener);
    return () => this.eventListeners.delete(listener);
  }

  onStateChange(listener: (state: AttachmentState) => void): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  /** Attach to ``sessionId``, or to a new conversation; then keep the file name current. */
  async start(sessionId?: SessionId): Promise<void> {
    setInterval(() => void this.reattachIfRenamed(), RENAME_POLL_MS);
    await this.open(sessionId);
  }

  /** Attach (again) to ``sessionId``; also how a displaced pane takes the document back. */
  async attach(sessionId: SessionId): Promise<void> {
    const before = this.state;
    // Not before this attach is answered: a renew from a reconnected socket is refused as NOT_ATTACHED,
    // which would attach a second time.
    this.cancelTimer();
    try {
      await this.attachTo(sessionId);
    } catch (error) {
      if (refusesConversation(error)) {
        await this.attachNewConversation(sessionId);
      } else if (this.state === before && before.kind === 'attached' && before.sessionId === sessionId) {
        // The attachment it had may still hold: keep renewing it; a renew refused NOT_ATTACHED attaches again.
        this.scheduleRenew(before.renewEverySeconds);
      } else {
        this.fail(sessionId, error);
      }
    }
  }

  /** Forget the calls of a turn the user cancelled: unstarted ones do not run, and none is answered. */
  cancelToolCalls(): void {
    this.options.executor.cancelAll();
    const sessionId = this.sessionId();
    for (const [requestId, call] of this.calls) {
      if (call.sessionId === sessionId) this.calls.delete(requestId);
    }
  }

  /** Attach to ``sessionId``, or to a new conversation when there is none. */
  private async open(sessionId: SessionId | undefined): Promise<void> {
    if (sessionId) return this.attach(sessionId);
    let fresh: SessionId;
    try {
      fresh = await this.options.createSession();
    } catch (error) {
      this.fail(undefined, error);
      return;
    }
    await this.attach(fresh);
  }

  private async attachNewConversation(refused: SessionId): Promise<void> {
    let fresh: SessionId | undefined;
    try {
      fresh = await this.options.createSession();
      await this.attachTo(fresh);
    } catch (error) {
      this.fail(fresh ?? refused, error);
    }
  }

  private fail(sessionId: SessionId | undefined, error: unknown): void {
    this.setState({ kind: 'failed', sessionId, error });
    const code = error instanceof GatewayRequestError ? error.code : undefined;
    if (code === undefined || code === 'UNAVAILABLE') {
      this.cancelTimer = this.timer(() => void this.open(sessionId), RETRY_SECONDS * 1000);
    }
  }

  /** ``stillCurrent`` false when the answer arrives: the pane moved on meanwhile, and the answer is dropped. */
  private async attachTo(sessionId: SessionId, stillCurrent: () => boolean = () => true): Promise<void> {
    const declaration = await this.options.declaration();
    const response = await this.options.gateway.request('client.attach', { session_id: sessionId, ...declaration });
    const attachmentId = response.attachment_id;
    if (typeof attachmentId !== 'string' || !attachmentId) {
      throw new GatewayRequestError('client.attach 未返回 attachment_id');
    }
    if (!stillCurrent()) return;
    const renewEverySeconds = renewInterval(response);
    this.setState({
      kind: 'attached', sessionId, attachmentId, displayName: declaration.resource.display_name, renewEverySeconds,
    });
    this.scheduleRenew(renewEverySeconds);
    const pending = Array.isArray(response.pending) ? response.pending.filter(isRecord) : [];
    // The pause hands out its client tool calls only while it waits on them.
    if (pending.some(item => item.event_type === 'chat.client_tool_request') && this.runningTurn === sessionId) {
      this.runningTurn = undefined;
    }
    for (const item of pending) {
      if (typeof item.event_type === 'string') this.deliver({ event: item.event_type, payload: item }, sessionId);
    }
    this.flush();
  }

  private scheduleRenew(seconds: number): void {
    this.cancelTimer();
    this.cancelTimer = this.timer(() => void this.renew(), seconds * 1000);
  }

  private async renew(): Promise<void> {
    const attached = this.state;
    if (attached.kind !== 'attached') return;
    try {
      const response = await this.options.gateway.request(
        'client.renew', { attachment_id: attached.attachmentId, session_id: attached.sessionId });
      if (this.state === attached) this.scheduleRenew(renewInterval(response));
    } catch (error) {
      if (this.state !== attached) return;
      const code = error instanceof GatewayRequestError ? error.code : undefined;
      if (code === 'NOT_ATTACHED') {
        void this.attach(attached.sessionId);
      } else if (code === 'DISPLACED') {
        this.setState({ kind: 'displaced', sessionId: attached.sessionId });
      } else {
        // Lost or late: the lease outlasts a few missed renews, and a reconnect attaches anyway.
        this.scheduleRenew(attached.renewEverySeconds);
      }
    }
  }

  private async reattachIfRenamed(): Promise<void> {
    const attached = this.state;
    if (attached.kind !== 'attached') return;
    let resource: AttachDeclaration['resource'];
    try {
      ({ resource } = await this.options.declaration());
    } catch {
      // The document could not be read just now; the next poll reads it again.
      return;
    }
    if (this.state !== attached || resource.display_name === attached.displayName) return;
    // Without ``attach``'s pause of the renews: this socket's attachment holds, and a failure leaves it as it is.
    try {
      await this.attachTo(attached.sessionId, () => this.state === attached);
    } catch (error) {
      if (refusesConversation(error) && this.state === attached) await this.attachNewConversation(attached.sessionId);
    }
  }

  private deliver(event: GatewayEvent, sessionId: SessionId): void {
    if (event.event === 'chat.client_tool_request') {
      // A displaced window still gets the calls of a turn it started, which the window holding the document
      // is never handed: it answers them so the turn goes on. A failed attach's calls wait for the attach.
      if (this.state.kind === 'attached') this.run(event.payload, sessionId);
      else if (this.state.kind === 'displaced') this.decline(event.payload, sessionId);
    } else if (event.event === 'chat.processing_status') {
      if (event.payload.is_processing === true) this.runningTurn = sessionId;
      else if (this.runningTurn === sessionId) this.runningTurn = undefined;
      this.flush();
    }
    this.eventListeners.forEach(listener => listener(event));
  }

  private run(payload: JsonObject, sessionId: SessionId): void {
    const request = parseClientToolRequest(payload);
    // Already running, or its answer is on the way: a call streamed and handed back by an attach runs once.
    if (!request || this.calls.has(request.requestId)) return;
    const running: Call = { sessionId, kind: 'running' };
    this.calls.set(request.requestId, running);
    void this.options.executor.execute(request).then(result => {
      if (this.calls.get(request.requestId) !== running) return;
      this.calls.set(request.requestId, { sessionId, kind: 'done', answer: answerOf(request, result) });
      this.flush();
    });
  }

  private decline(payload: JsonObject, sessionId: SessionId): void {
    const request = parseClientToolRequest(payload);
    if (!request || this.calls.has(request.requestId)) return;
    const result = this.options.executor.declineHeldElsewhere(request);
    this.calls.set(request.requestId, { sessionId, kind: 'done', answer: answerOf(request, result) });
    this.flush();
  }

  /** Answer the finished calls of the conversation the pane shows, unless its turn is running. */
  private flush(): void {
    const { state } = this;
    if (state.kind !== 'attached' && state.kind !== 'displaced') return;
    const { sessionId } = state;
    if (this.runningTurn === sessionId) return;
    const sending = new Map<string, Call & { kind: 'sending' }>();
    for (const [requestId, call] of this.calls) {
      if (call.kind === 'done' && call.sessionId === sessionId) {
        sending.set(requestId, { sessionId, kind: 'sending', answer: call.answer });
      }
    }
    if (!sending.size) return;
    sending.forEach((call, requestId) => this.calls.set(requestId, call));
    // The answers resume the turn.
    this.runningTurn = sessionId;
    this.options.gateway.request('chat.send', {
      session_id: sessionId,
      query: '',
      mode: 'agent.work.normal',
      work_mode: 'work',
      source: 'client_tool_interrupt',
      answers: [...sending.values()].map(call => call.answer),
    }).then(
      // Taken: the backend resumes with them, or refuses them as answers to calls it no longer waits on.
      () => sending.forEach((call, requestId) => {
        if (this.calls.get(requestId) === call) this.calls.delete(requestId);
      }),
      // Not delivered: answered again on the next end of a turn or attach.
      () => {
        if (this.runningTurn === sessionId) this.runningTurn = undefined;
        sending.forEach((call, requestId) => {
          if (this.calls.get(requestId) === call) this.calls.set(requestId, { ...call, kind: 'done' });
        });
      },
    );
  }

  private setState(state: AttachmentState): void {
    if (state.kind !== 'attached') this.cancelTimer();
    this.state = state;
    this.stateListeners.forEach(listener => listener(state));
  }
}

/** A refusal after which the conversation is not this resource's to attach: start a new one. */
function refusesConversation(error: unknown): boolean {
  return error instanceof GatewayRequestError && NEW_CONVERSATION_CODES.has(error.code ?? '');
}

function answerOf(request: ClientToolRequest, result: ClientToolResult): ClientToolAnswer {
  return { request_id: request.requestId, interrupt_id: request.interruptId, result };
}

function renewInterval(response: JsonObject): number {
  const seconds = response.renew_every_seconds;
  if (typeof seconds !== 'number' || !(seconds > 0)) {
    throw new GatewayRequestError('client.attach 未返回 renew_every_seconds');
  }
  return seconds;
}

/** A ``chat.client_tool_request`` payload as one call, or undefined when it names no call to answer. */
function parseClientToolRequest(payload: JsonObject): ClientToolRequest | undefined {
  const { request_id: requestId, interrupt_id: interruptId, tool_name: toolName, arguments: input } = payload;
  if (typeof requestId !== 'string' || !requestId || typeof interruptId !== 'string' || !interruptId
    || typeof toolName !== 'string' || !isRecord(input)) return undefined;
  const offered = isRecord(payload.client_tool) && isRecord(payload.client_tool.downloads)
    ? payload.client_tool.downloads : {};
  const downloads = Object.fromEntries(
    Object.entries(offered).filter((entry): entry is [string, string] => typeof entry[1] === 'string'));
  return { requestId, interruptId, toolName, input, downloads };
}
