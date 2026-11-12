import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ClientAttachment, type SessionId } from './clientAttachment';
import type { DocumentId } from './documentIdentity';
import { GatewayRequestError, type GatewayEvent, type JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { PowerPointToolDispatcher, type PresentationToolRuntime } from './powerPointToolDispatcher';
import { presentationAttachDeclaration } from './presentationContract';
import { PRESENTATION_TOOL_NAMES, type ClientToolResponse } from './presentationToolTypes';
import type { Timer } from './workerTimer';

const DOCUMENT = '3f1c2a9e-7b4d-4e8a-9c21-5d6f7a8b9c0d' as DocumentId;
const S1 = 's1' as SessionId;
const S2 = 's2' as SessionId;
const POWERPOINT_1_4 = { isSetSupported: (_: string, version: string) => Number(version) <= 1.4, worker: false };
const ALL_TOOLS = new Set(PRESENTATION_TOOL_NAMES);
/** The page's own timers, which the tests' fake clock drives. */
const pageTimer: Timer = (callback, ms) => {
  const timer = setTimeout(callback, ms);
  return () => clearTimeout(timer);
};

type Answer = JsonObject | GatewayRequestError | ((params: JsonObject) => JsonObject | Promise<JsonObject>);

/** The Gateway as the pane sees it: answers scripted per method, events and reconnects pushed by the test. */
class FakeGateway {
  readonly requests: { method: string; params: JsonObject }[] = [];
  private readonly answers = new Map<string, Answer[]>();
  private eventListener: (event: GatewayEvent) => void = () => undefined;
  private reconnectListener: () => void = () => undefined;

  /** Answers of ``method``, in order; the last one repeats. */
  answer(method: string, ...answers: Answer[]): void {
    this.answers.set(method, answers);
  }

  readonly request = async (method: string, params: JsonObject = {}): Promise<JsonObject> => {
    this.requests.push({ method, params });
    const queue = this.answers.get(method) ?? [{}];
    const next = queue.length > 1 ? queue.shift()! : queue[0];
    if (next instanceof GatewayRequestError) throw next;
    return typeof next === 'function' ? next(params) : next;
  };

  onEvent(listener: (event: GatewayEvent) => void): () => void {
    this.eventListener = listener;
    return () => undefined;
  }

  onReconnect(listener: () => void): () => void {
    this.reconnectListener = listener;
    return () => undefined;
  }

  emit(event: string, payload: JsonObject): void {
    this.eventListener({ event, payload });
  }

  reconnect(): void {
    this.reconnectListener();
  }

  sent(method: string): JsonObject[] {
    return this.requests.filter(request => request.method === method).map(request => request.params);
  }

  methods(): string[] {
    return this.requests.map(request => request.method);
  }
}

function attached(overrides: JsonObject = {}): JsonObject {
  return { attachment_id: 'att_1', session_id: 's1', client_app: 'office-powerpoint', pending: [],
    lease_seconds: 120, renew_every_seconds: 30, ...overrides };
}

/** A ``chat.client_tool_request`` as the backend streams it (runtime/client_tools.py). */
function toolRequest(overrides: JsonObject = {}): JsonObject {
  return {
    event_type: 'chat.client_tool_request', request_id: 'call_1', interrupt_id: 'int_1',
    source: 'client_tool_interrupt', tool_name: 'read_presentation_context', arguments: {},
    client_tool: { downloads: {} }, ...overrides,
  };
}

const settle = () => vi.advanceTimersByTimeAsync(0);

describe('ClientAttachment', () => {
  let gateway: FakeGateway;
  let runtime: { tools: typeof ALL_TOOLS; execute: ReturnType<typeof vi.fn<PresentationToolRuntime['execute']>> };
  let displayName: string;
  let createSession: ReturnType<typeof vi.fn<() => Promise<SessionId>>>;
  let attachment: ClientAttachment;
  const attachmentWith = (renewTimer: Timer) => new ClientAttachment({
    gateway,
    executor: new PowerPointToolDispatcher(runtime, new HostWriteQueue()),
    declaration: async () => presentationAttachDeclaration(
      { documentId: DOCUMENT, displayName, host: POWERPOINT_1_4, implemented: ALL_TOOLS }),
    createSession,
    renewTimer,
  });

  beforeEach(() => {
    vi.useFakeTimers();
    gateway = new FakeGateway();
    gateway.answer('client.attach', attached());
    gateway.answer('client.renew', { lease_seconds: 120, renew_every_seconds: 30 });
    gateway.answer('chat.send', { accepted: true });
    runtime = {
      tools: ALL_TOOLS,
      execute: vi.fn<PresentationToolRuntime['execute']>(async () => ({ ok: true, result: { slide_count: 3 } })),
    };
    displayName = 'Deck.pptx';
    createSession = vi.fn(async () => S2);
    attachment = attachmentWith(pageTimer);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('attaches the document to the conversation, then renews as often as each answer says', async () => {
    gateway.answer('client.attach', attached({ renew_every_seconds: 7 }));
    gateway.answer('client.renew', { renew_every_seconds: 11 });
    await attachment.start(S1);

    expect(gateway.sent('client.attach')).toEqual([{
      session_id: 's1',
      resource: { key: DOCUMENT, display_name: 'Deck.pptx', kind: 'presentation', host_vendor: 'microsoft' },
      capabilities: { supported_tools: ['read_presentation_context', 'list_slide_shapes', 'verify_slides'] },
    }]);
    expect(attachment.current()).toMatchObject({ kind: 'attached', sessionId: 's1', attachmentId: 'att_1' });
    await vi.advanceTimersByTimeAsync(6_999);
    expect(gateway.sent('client.renew')).toEqual([]);
    await vi.advanceTimersByTimeAsync(1);
    expect(gateway.sent('client.renew')).toEqual([{ attachment_id: 'att_1', session_id: 's1' }]);
    await vi.advanceTimersByTimeAsync(10_999);
    expect(gateway.sent('client.renew')).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(1);
    expect(gateway.sent('client.renew')).toHaveLength(2);
  });

  it('renews on its own timer while the hidden pane\'s timers are held back', async () => {
    const due: (() => void)[] = [];
    attachment = attachmentWith(callback => {
      due.push(callback);
      return () => due.splice(due.indexOf(callback), 1);
    });
    await attachment.start(S1);
    expect(due).toHaveLength(1);
    due.shift()!();
    await settle();
    expect(gateway.sent('client.renew')).toEqual([{ attachment_id: 'att_1', session_id: 's1' }]);
    expect(due).toHaveLength(1);
  });

  it('attaches again when a renew finds no attachment', async () => {
    gateway.answer('client.renew', new GatewayRequestError('no such attachment: attach again', 'NOT_ATTACHED'));
    await attachment.start(S1);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(gateway.methods()).toEqual(['client.attach', 'client.renew', 'client.attach']);
    expect(attachment.current()).toMatchObject({ kind: 'attached', sessionId: 's1' });
  });

  it('keeps renewing after a renew that got no answer', async () => {
    gateway.answer('client.renew', new GatewayRequestError('client.renew 请求超时'), { renew_every_seconds: 30 });
    await attachment.start(S1);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(gateway.methods()).toEqual(['client.attach', 'client.renew', 'client.renew']);
  });

  it('attaches right away after a reconnect, without trying the old socket\'s renew', async () => {
    await attachment.start(S1);
    await vi.advanceTimersByTimeAsync(10_000);
    gateway.reconnect();
    await settle();
    expect(gateway.methods()).toEqual(['client.attach', 'client.attach']);
    await vi.advanceTimersByTimeAsync(29_999);
    expect(gateway.sent('client.renew')).toEqual([]);
  });

  it('sends no renew from the new socket before its attach is answered', async () => {
    let answerAttach: (answer: JsonObject) => void = () => undefined;
    gateway.answer('client.attach', attached(), () => new Promise<JsonObject>(resolve => { answerAttach = resolve; }));
    await attachment.start(S1);
    await vi.advanceTimersByTimeAsync(29_000);
    gateway.reconnect();
    await vi.advanceTimersByTimeAsync(5_000);
    expect(gateway.methods()).toEqual(['client.attach', 'client.attach']);
    answerAttach(attached());
    await vi.advanceTimersByTimeAsync(30_000);
    expect(gateway.methods()).toEqual(['client.attach', 'client.attach', 'client.renew']);
  });

  it.each(['CONFLICT', 'NOT_FOUND_OR_FORBIDDEN'])(
    'starts a new conversation for the document when the attach is refused with %s',
    async code => {
      gateway.answer('client.attach', new GatewayRequestError('refused', code), attached({ session_id: 's2' }));
      await attachment.start(S1);
      expect(createSession).toHaveBeenCalledOnce();
      expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s1', 's2']);
      expect(attachment.current()).toMatchObject({ kind: 'attached', sessionId: 's2' });
    },
  );

  it('starts a new conversation when it has none', async () => {
    await attachment.start();
    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s2']);
  });

  it.each([
    ['UNAVAILABLE', new GatewayRequestError('the client could not attach', 'UNAVAILABLE')],
    ['no answer', new GatewayRequestError('client.attach 请求超时')],
  ])('shows an attach that failed with %s, and tries again as often as a renew', async (_, failure) => {
    gateway.answer('client.attach', failure, attached());
    await attachment.start(S1);
    expect(createSession).not.toHaveBeenCalled();
    expect(attachment.current()).toMatchObject({ kind: 'failed', sessionId: 's1' });
    await vi.advanceTimersByTimeAsync(29_999);
    expect(gateway.methods()).toEqual(['client.attach']);
    await vi.advanceTimersByTimeAsync(1);
    expect(gateway.methods()).toEqual(['client.attach', 'client.attach']);
    expect(attachment.current()).toMatchObject({ kind: 'attached', sessionId: 's1' });
  });

  it('shows any other refusal, and attaches again on the next reconnect', async () => {
    gateway.answer('client.attach', new GatewayRequestError('no such app', 'APP_UNAVAILABLE'), attached());
    await attachment.start(S1);
    expect(attachment.current()).toMatchObject({ kind: 'failed', sessionId: 's1' });
    await vi.advanceTimersByTimeAsync(120_000);
    expect(gateway.methods()).toEqual(['client.attach']);
    gateway.reconnect();
    await settle();
    expect(attachment.current()).toMatchObject({ kind: 'attached', sessionId: 's1' });
  });

  it('starts a new conversation on the next reconnect when it could not start one', async () => {
    createSession.mockRejectedValueOnce(new GatewayRequestError('session.create failed', 'INTERNAL'));
    await attachment.start();
    expect(attachment.current()).toMatchObject({ kind: 'failed', sessionId: undefined });
    gateway.reconnect();
    await settle();
    expect(createSession).toHaveBeenCalledTimes(2);
    expect(attachment.current()).toMatchObject({ kind: 'attached', sessionId: 's2' });
  });

  it.each([
    ['the file is renamed', async () => {
      displayName = 'Q3 Review.pptx';
      await vi.advanceTimersByTimeAsync(5_000);
    }],
    ['a renew finds no attachment', async () => {
      gateway.answer('client.renew', new GatewayRequestError('no such attachment: attach again', 'NOT_ATTACHED'),
        { renew_every_seconds: 30 });
      await vi.advanceTimersByTimeAsync(30_000);
    }],
  ])('keeps the attachment and its renews when attaching again after %s fails', async (_, attachAgain) => {
    const states: string[] = [];
    await attachment.start(S1);
    attachment.onStateChange(state => states.push(state.kind));
    gateway.answer('client.attach', new GatewayRequestError('client.attach 请求超时'));
    await attachAgain();
    expect(gateway.sent('client.attach')).toHaveLength(2);
    expect(attachment.current()).toMatchObject({ kind: 'attached', sessionId: 's1', attachmentId: 'att_1' });
    expect(states).toEqual([]);
    const renews = gateway.sent('client.renew').length;
    await vi.advanceTimersByTimeAsync(30_000);
    expect(gateway.sent('client.renew')).toHaveLength(renews + 1);
  });

  it('keeps the attachment when the document cannot be read for the rename check', async () => {
    const unhandled: unknown[] = [];
    const onUnhandled = (reason: unknown) => unhandled.push(reason);
    process.on('unhandledRejection', onUnhandled);
    let reads = 0;
    attachment = new ClientAttachment({
      gateway,
      executor: new PowerPointToolDispatcher(runtime, new HostWriteQueue()),
      declaration: async () => {
        reads += 1;
        if (reads === 2) throw new Error('Office settings unavailable');
        return presentationAttachDeclaration(
          { documentId: DOCUMENT, displayName, host: POWERPOINT_1_4, implemented: ALL_TOOLS });
      },
      createSession,
      renewTimer: pageTimer,
    });
    try {
      await attachment.start(S1);
      await vi.advanceTimersByTimeAsync(5_000);
      expect(unhandled).toEqual([]);
      expect(attachment.current()).toMatchObject({ kind: 'attached', sessionId: 's1' });
      displayName = 'Q3 Review.pptx';
      await vi.advanceTimersByTimeAsync(5_000);
      expect(gateway.sent('client.attach')).toHaveLength(2);
    } finally {
      process.off('unhandledRejection', onUnhandled);
    }
  });

  it('stops renewing when another window holds the document, until this one takes it back', async () => {
    const states: string[] = [];
    attachment.onStateChange(state => states.push(state.kind));
    gateway.answer('client.renew', new GatewayRequestError('held by another client', 'DISPLACED'), {});
    await attachment.start(S1);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(attachment.current()).toEqual({ kind: 'displaced', sessionId: 's1' });
    await vi.advanceTimersByTimeAsync(300_000);
    gateway.reconnect();
    await settle();
    expect(gateway.methods()).toEqual(['client.attach', 'client.renew']);

    await attachment.attach(S1);
    expect(states).toEqual(['attached', 'displaced', 'attached']);
    await vi.advanceTimersByTimeAsync(30_000);
    expect(gateway.sent('client.renew')).toHaveLength(2);
  });

  it('attaches again under the new name when the file is renamed', async () => {
    await attachment.start(S1);
    displayName = 'Q3 Review.pptx';
    await vi.advanceTimersByTimeAsync(5_000);
    expect(gateway.sent('client.attach').map(params => (params.resource as JsonObject).display_name))
      .toEqual(['Deck.pptx', 'Q3 Review.pptx']);
    await vi.advanceTimersByTimeAsync(5_000);
    expect(gateway.sent('client.attach')).toHaveLength(2);
  });

  it('drops a rename attach answered after the pane moved to another conversation', async () => {
    await attachment.start(S1);
    let answerRename: (value: JsonObject) => void = () => undefined;
    gateway.answer('client.attach', params => (params.session_id === 's1'
      ? new Promise<JsonObject>(resolve => { answerRename = resolve; })
      : attached({ session_id: 's2', attachment_id: 'att_2' })));
    displayName = 'Q3 Review.pptx';
    await vi.advanceTimersByTimeAsync(5_000);
    await attachment.attach(S2);
    answerRename(attached());
    await settle();
    expect(attachment.current()).toMatchObject({ kind: 'attached', sessionId: 's2', attachmentId: 'att_2' });
  });

  it('stays displaced when a rename attach is answered after a renew found another window', async () => {
    gateway.answer('client.renew', new GatewayRequestError('held by another client', 'DISPLACED'), {});
    await attachment.start(S1);
    let answerRename: (value: JsonObject) => void = () => undefined;
    gateway.answer('client.attach', () => new Promise<JsonObject>(resolve => { answerRename = resolve; }));
    displayName = 'Q3 Review.pptx';
    await vi.advanceTimersByTimeAsync(30_000);
    expect(attachment.current()).toEqual({ kind: 'displaced', sessionId: 's1' });
    answerRename(attached());
    await settle();
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's1' }));
    await settle();
    expect(attachment.current()).toEqual({ kind: 'displaced', sessionId: 's1' });
    expect(runtime.execute).not.toHaveBeenCalled();
  });

  it('answers only while the turn is not running, each field as the backend reads it', async () => {
    await attachment.start(S1);
    gateway.emit('chat.processing_status', { session_id: 's1', is_processing: true });
    gateway.emit('chat.client_tool_request', toolRequest({
      session_id: 's1', request_id: 'call_7:target', interrupt_id: 'int_7',
      client_tool: { downloads: { oa_1: '/file-api/download?token=t' } },
    }));
    await settle();
    expect(runtime.execute).toHaveBeenCalledWith({ callId: 'call_7:target',
      toolName: 'read_presentation_context', input: {}, downloads: { oa_1: '/file-api/download?token=t' },
    });
    expect(gateway.sent('chat.send')).toEqual([]);

    gateway.emit('chat.processing_status', { session_id: 's1', is_processing: false });
    await settle();
    expect(gateway.sent('chat.send')).toEqual([{
      session_id: 's1',
      query: '',
      mode: 'agent.work.normal',
      work_mode: 'work',
      source: 'client_tool_interrupt',
      answers: [{
        request_id: 'call_7:target',
        interrupt_id: 'int_7',
        result: {
          status: 'completed',
          content: [{ type: 'text', text: '{"slide_count":3}' }],
          is_error: false,
          data: { telemetry: { elapsed_ms: 0, queue_wait_ms: 0, host: {} } },
        },
      }],
    }]);
  });

  it('runs a call that streams and comes back with an attach once; hands on the pending questions', async () => {
    const events: GatewayEvent[] = [];
    attachment.onEvent(event => events.push(event));
    const question = { event_type: 'chat.ask_user_question', request_id: 'perm_1', source: 'permission_interrupt' };
    gateway.answer('client.attach', attached(), attached({ pending: [toolRequest(), question] }));
    await attachment.start(S1);
    gateway.emit('chat.processing_status', { session_id: 's1', is_processing: true });
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's1' }));
    gateway.reconnect();
    await settle();

    expect(runtime.execute).toHaveBeenCalledOnce();
    expect(gateway.sent('chat.send')).toHaveLength(1);
    expect((gateway.sent('chat.send')[0].answers as JsonObject[]).map(answer => answer.request_id)).toEqual(['call_1']);
    expect(events.map(event => event.event)).toContain('chat.ask_user_question');
  });

  it('answers a call as run in the other window once another window holds the document', async () => {
    gateway.answer('client.renew', new GatewayRequestError('held by another client', 'DISPLACED'), {});
    await attachment.start(S1);
    await vi.advanceTimersByTimeAsync(30_000);
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's1', tool_name: 'edit_slide_text' }));
    await settle();
    expect(attachment.current()).toEqual({ kind: 'displaced', sessionId: 's1' });
    expect(runtime.execute).not.toHaveBeenCalled();
    const [send] = gateway.sent('chat.send');
    expect(send).toMatchObject({ session_id: 's1', source: 'client_tool_interrupt' });
    const [answer] = send.answers as { request_id: string; result: { status: string; is_error: boolean;
      content: { text: string }[] } }[];
    expect(answer).toMatchObject({ request_id: 'call_1', result: { status: 'error', is_error: true } });
    expect(JSON.parse(answer.result.content[0].text)).toMatchObject({
      success: false, code: 'CLIENT_TOOL_DISPLACED', retryable: false,
      details: { outcome: 'not_applied', native_mutation_started: false },
    });
    expect(answer.result.content[0].text).toContain('open in another window');
  });

  it('neither runs nor answers a call while its attach failed, and runs it once the attach hands it back', async () => {
    gateway.answer('client.attach', new GatewayRequestError('the client could not attach', 'UNAVAILABLE'),
      attached({ pending: [toolRequest()] }));
    await attachment.start(S1);
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's1' }));
    await settle();
    expect(runtime.execute).not.toHaveBeenCalled();
    expect(gateway.sent('chat.send')).toEqual([]);

    await vi.advanceTimersByTimeAsync(30_000);
    expect(runtime.execute).toHaveBeenCalledOnce();
    expect((gateway.sent('chat.send')[0].answers as JsonObject[])[0]).toMatchObject(
      { request_id: 'call_1', result: { status: 'completed' } });
  });

  it('answers a call in its own conversation after the pane moved to another, without running it again', async () => {
    let finish: (response: ClientToolResponse) => void = () => undefined;
    runtime.execute.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    await attachment.start(S1);
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's1' }));
    await settle();
    await attachment.attach(S2);
    finish({ ok: true, result: {} });
    await settle();
    expect(gateway.sent('chat.send')).toEqual([]);

    gateway.answer('client.attach', attached({ pending: [toolRequest()] }));
    await attachment.attach(S1);
    await settle();
    expect(runtime.execute).toHaveBeenCalledOnce();
    expect(gateway.sent('chat.send').map(send => [send.session_id, (send.answers as JsonObject[])
      .map(answer => answer.request_id)])).toEqual([['s1', ['call_1']]]);
  });

  it('keeps a conversation the pane left out of the next one\'s turn and cancel', async () => {
    let finish: (response: ClientToolResponse) => void = () => undefined;
    runtime.execute.mockReturnValueOnce(new Promise(resolve => { finish = resolve; }));
    await attachment.start(S1);
    gateway.emit('chat.processing_status', { session_id: 's1', is_processing: true });
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's1' }));
    await settle();
    await attachment.attach(S2);
    finish({ ok: true, result: {} });
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's2', request_id: 'call_2' }));
    await settle();
    expect(gateway.sent('chat.send').map(send => [send.session_id, (send.answers as JsonObject[])
      .map(answer => answer.request_id)])).toEqual([['s2', ['call_2']]]);

    attachment.cancelToolCalls();
    gateway.answer('client.attach', attached({ pending: [toolRequest()] }));
    await attachment.attach(S1);
    await settle();
    expect(runtime.execute).toHaveBeenCalledTimes(2);
    expect(gateway.sent('chat.send').map(send => send.session_id)).toEqual(['s2', 's1']);
  });

  it('answers a call whose result does not serialize as failed', async () => {
    runtime.execute.mockResolvedValueOnce({ ok: true, result: { n: 1n } });
    await attachment.start(S1);
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's1' }));
    await settle();
    const [answer] = gateway.sent('chat.send')[0].answers as { result: { status: string; content: { text: string }[] }
    }[];
    expect(answer.result.status).toBe('error');
    expect(JSON.parse(answer.result.content[0].text)).toMatchObject({ code: 'CLIENT_TOOL_RESULT_UNREADABLE' });
  });

  it('drops the events of other conversations', async () => {
    const events: string[] = [];
    attachment.onEvent(event => events.push(event.event));
    await attachment.start(S1);
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's-other' }));
    gateway.emit('chat.delta', { session_id: 's-other', content: 'x' });
    gateway.emit('chat.delta', { content: 'no conversation' });
    gateway.emit('models.updated', {});
    gateway.emit('chat.delta', { session_id: 's1', content: 'y' });
    await settle();
    expect(runtime.execute).not.toHaveBeenCalled();
    expect(events).toEqual(['models.updated', 'chat.delta']);
  });

  it('answers again, without running the call again, when the resume did not reach the Gateway', async () => {
    gateway.answer('chat.send', new GatewayRequestError('Gateway connection closed'), { accepted: true });
    gateway.answer('client.attach', attached(), attached({ pending: [toolRequest()] }));
    await attachment.start(S1);
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's1' }));
    await settle();
    expect(gateway.sent('chat.send')).toHaveLength(1);

    gateway.reconnect();
    await settle();
    expect(runtime.execute).toHaveBeenCalledOnce();
    expect(gateway.sent('chat.send')).toHaveLength(2);
    expect(gateway.sent('chat.send')[1]).toEqual(gateway.sent('chat.send')[0]);
  });

  it('does not answer the calls of a turn the user cancelled', async () => {
    let finish: (response: ClientToolResponse) => void = () => undefined;
    runtime.execute.mockReturnValue(new Promise(resolve => { finish = resolve; }));
    await attachment.start(S1);
    gateway.emit('chat.client_tool_request', toolRequest({ session_id: 's1' }));
    await settle();
    attachment.cancelToolCalls();
    finish({ ok: true, result: {} });
    gateway.emit('chat.processing_status', { session_id: 's1', is_processing: false });
    await settle();
    expect(gateway.sent('chat.send')).toEqual([]);
  });
});
