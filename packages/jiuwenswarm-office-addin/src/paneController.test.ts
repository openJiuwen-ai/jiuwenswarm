import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { attachDeclaration } from './appContract';
import { ClientAttachment, type SessionId } from './clientAttachment';
import { ClientToolDispatcher, type ClientToolRuntime } from './clientToolDispatcher';
import type { Entry } from './conversationModel';
import type { DocumentId } from './documentIdentity';
import { GatewayRequestError, type JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { TaskPaneController } from './paneController';
import { POWERPOINT } from './powerPointApp';
import { ScriptedGateway } from './scriptedGateway';
import { SessionFacade } from './sessionFacade';
import type { SlideSnapshots, UndoResult } from './slideSnapshots';
import type { Timer } from './workerTimer';

const DOCUMENT = '3f1c2a9e-7b4d-4e8a-9c21-5d6f7a8b9c0d' as DocumentId;
const pageTimer: Timer = (callback, ms) => {
  const timer = setTimeout(callback, ms);
  return () => clearTimeout(timer);
};
const PERMISSION: JsonObject = {
  request_id: 'call_7', interrupt_id: 'int_7', source: 'permission_interrupt',
  questions: [{ question: '允许发送任务吗？', header: '权限审批', card_id: 'call_7',
    options: [{ value: 'allow_once', label: '本次允许' }, { value: 'reject', label: '拒绝' }] }],
};

let gateway: ScriptedGateway;
let execute: ReturnType<typeof vi.fn<ClientToolRuntime['execute']>>;
let snapshots: {
  closeTurn: ReturnType<typeof vi.fn<SlideSnapshots['closeTurn']>>;
  undoable: ReturnType<typeof vi.fn<SlideSnapshots['undoable']>>;
  undo: ReturnType<typeof vi.fn<SlideSnapshots['undo']>>;
};

/** The pane as the task pane wires it, against a scripted Gateway and a stand-in PowerPoint. */
function pane(): TaskPaneController {
  execute = vi.fn<ClientToolRuntime['execute']>(async () => ({ ok: true, result: { slide_count: 3 } }));
  const runtime: ClientToolRuntime = { tools: new Set(['read_presentation_context']), execute };
  const sessions = new SessionFacade(gateway, POWERPOINT.clientApp);
  const attachment = new ClientAttachment({
    gateway,
    executor: new ClientToolDispatcher(POWERPOINT, runtime, new HostWriteQueue()),
    declaration: async () => attachDeclaration(POWERPOINT, {
      documentId: DOCUMENT, displayName: '季度汇报.pptx', host: { isSetSupported: () => true, worker: true },
      implemented: runtime.tools,
    }),
    createSession: async () => sessions.create(await sessions.defaultModel()),
    renewTimer: pageTimer,
  });
  snapshots = {
    closeTurn: vi.fn(async () => undefined),
    undoable: vi.fn(() => ({ slides: 2 })),
    undo: vi.fn(async (): Promise<UndoResult> => ({ kind: 'undone', turns: 1, positions: [2, 3] })),
  };
  return new TaskPaneController(
    { attachment, sessions, documentId: DOCUMENT, connection: gateway, contract: POWERPOINT.contract, snapshots });
}

const settle = async (): Promise<void> => {
  await vi.advanceTimersByTimeAsync(1);
};

function transcript(controller: TaskPaneController): Omit<Entry, 'id'>[] {
  return controller.state().conversation.entries.map(({ id: _id, ...entry }) => entry);
}

function emit(event: string, payload: JsonObject, sessionId = 's1'): void {
  gateway.emit(event, { session_id: sessionId, ...payload });
}

beforeEach(() => {
  vi.useFakeTimers();
  gateway = new ScriptedGateway();
  // As the backend's: one id for every attach of the document by its user.
  gateway.answer('client.attach', { attachment_id: 'a1', renew_every_seconds: 30, pending: [] });
  gateway.answer('client.conversations', { conversations: [
    { session_id: 's1', title: '改标题', last_message_at: 200 }, { session_id: 's0', title: '旧的', last_message_at: 100 },
  ] });
  gateway.answer('client.conversation.messages', params => ({ messages: params.target_session_id === 's1'
    ? [{ role: 'user', content: '改标题', request_id: 'r1' }, { role: 'assistant', content: '好了。', request_id: 'r1' }]
    : [] }));
  gateway.answer('client.list', { attachments: [{ session_id: 's9', display_name: '市场分析.pptx' }] });
  gateway.answer('models.list', { active_model: 'm-active', models: [{ model_name: 'm-other' }] });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('opening the pane', () => {
  it('attaches the document\'s most recent conversation and shows its history', async () => {
    const controller = pane();
    await controller.open();
    await settle();

    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s1']);
    expect(gateway.sent('session.create')).toEqual([]);
    expect(controller.state().conversations.map(item => item.sessionId)).toEqual(['s1', 's0']);
    expect(transcript(controller)).toEqual([{ kind: 'user', text: '改标题' }, { kind: 'assistant', text: '好了。' }]);
    expect(controller.state().documents.get('s9')?.name).toBe('市场分析.pptx');
  });

  it('starts a conversation on the server\'s active model when the document has none', async () => {
    gateway.answer('client.conversations', { conversations: [] });
    gateway.answer('session.create', { session_id: 's5' });
    const controller = pane();
    await controller.open();
    await settle();

    expect(gateway.sent('session.create')[0]).toMatchObject({ model_name: 'm-active', client_app: 'office-powerpoint' });
    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s5']);
  });

  it('shows the turn the server still runs as running, so it can be stopped', async () => {
    gateway.answer('session.get_metadata', { session_id: 's1', is_processing: true, runtime: {} });
    const controller = pane();
    await controller.open();
    await settle();

    expect(controller.state().conversation.turn).toBe('running');
    expect(transcript(controller)).toEqual([{ kind: 'user', text: '改标题' }, { kind: 'assistant', text: '好了。' }]);
  });

  it('applies what streams in while the history loads after the history', async () => {
    let release: (value: JsonObject) => void = () => undefined;
    gateway.answer('client.conversation.messages', () => new Promise<JsonObject>(resolve => { release = resolve; }));
    const controller = pane();
    await controller.open();
    emit('chat.processing_status', { is_processing: true });
    emit('chat.delta', { content: '继续' });
    expect(controller.state().loadingHistory).toBe(true);

    release({ messages: [{ role: 'user', content: '改标题' }] });
    await settle();
    expect(transcript(controller)).toEqual([{ kind: 'user', text: '改标题' }, { kind: 'assistant', text: '继续' }]);
    expect(controller.state().conversation.turn).toBe('running');
  });
});

describe('chatting', () => {
  it('sends a message to the attached conversation; one sent during a turn waits its turn, in order', async () => {
    const controller = pane();
    await controller.open();
    await settle();

    expect(await controller.send(' 检查标题 ')).toBe(true);
    expect(gateway.sent('chat.send')).toEqual([expect.objectContaining({ session_id: 's1', content: '检查标题' })]);
    expect(controller.state().conversation.turn).toBe('running');

    expect(await controller.send('只看前 3 页')).toBe(true);
    expect(await controller.send('再统一字号')).toBe(true);
    expect(controller.state().queue.map(item => item.text)).toEqual(['只看前 3 页', '再统一字号']);
    expect(gateway.sent('chat.interrupt')).toEqual([]);
    expect(gateway.sent('chat.send')).toHaveLength(1);
    emit('chat.processing_status', { is_processing: false });
    await settle();
    expect(gateway.sent('chat.send').map(params => params.content)).toEqual(['检查标题', '只看前 3 页']);
    expect(controller.state().queue.map(item => item.text)).toEqual(['再统一字号']);
    expect(transcript(controller).filter(entry => entry.kind === 'user')).toEqual(
      ['改标题', '检查标题', '只看前 3 页'].map(text => ({ kind: 'user', text })));
  });

  it('sends the images picked with the message, and shows them in it; during a turn they wait with it', async () => {
    const path = { filename: 'logo.png', mime_type: 'image/png', path: '/u/logo.png' };
    gateway.answer('media.persist', { media_items: [{ type: 'image', ...path }] });
    const controller = pane();
    await controller.open();
    await settle();
    const logo = { filename: 'logo.png', mimeType: 'image/png', base64Data: 'QUJD' };

    expect(await controller.send('', [logo])).toBe(true);
    expect(gateway.sent('chat.send')).toEqual(
      [expect.objectContaining({ content: '', files: { uploaded_images: [path] } })]);
    expect(transcript(controller).at(-1)).toEqual(
      { kind: 'user', text: '', images: [{ filename: 'logo.png', src: 'data:image/png;base64,QUJD' }] });

    expect(controller.state().conversation.turn).toBe('running');
    expect(await controller.send('照这个改', [logo])).toBe(true);
    expect(controller.state().queue).toEqual([expect.objectContaining({ text: '照这个改', attachments: [logo] })]);
  });

  it('sends a document with the message, naming its path to the model as the web chat does', async () => {
    const stored = { type: 'document', filename: 'brief.pdf', mime_type: 'application/pdf', path: '/u/my brief.pdf',
      original_path: '/u/my brief.pdf', size_bytes: 3 };
    gateway.answer('document.persist', params => (
      (params.documents as JsonObject[])[0].filename === 'brief.pdf'
        ? { media_items: [stored] } : { document_errors: [{ index: 0, error: 'Forbidden document type' }] }));
    const controller = pane();
    await controller.open();
    await settle();
    const brief = { filename: 'brief.pdf', mimeType: 'application/pdf', base64Data: 'QUJD' };

    expect(await controller.send('总结一下', [brief])).toBe(true);
    expect(gateway.sent('document.persist')[0]).toEqual({ session_id: 's1', content: '',
      documents: [{ filename: 'brief.pdf', mime_type: 'application/pdf', base64_data: 'QUJD', size_bytes: 3 }] });
    expect(gateway.sent('chat.send')[0]).toMatchObject({
      content: '总结一下\n【上传文档】\n@"/u/my brief.pdf"', media_items: [stored],
      files: { uploaded_documents: [
        { filename: 'brief.pdf', path: '/u/my brief.pdf', mime_type: 'application/pdf', size_bytes: 3 }] },
    });
    expect(transcript(controller).at(-1)).toEqual(
      { kind: 'user', text: '总结一下', files: [{ filename: 'brief.pdf', size: 3 }] });

    emit('chat.processing_status', { is_processing: false });
    expect(await controller.send('', [{ ...brief, filename: 'tool.pdf' }])).toBe(false);
    expect(controller.state().alert?.text).toBe('发送失败：tool.pdf：Forbidden document type');
  });

  it('says why a message did not go out, takes it off the transcript and leaves the turn idle', async () => {
    gateway.answer('chat.send', new GatewayRequestError('连接已断开'));
    const controller = pane();
    await controller.open();
    await settle();

    expect(await controller.send('检查标题')).toBe(false);
    expect(controller.state().conversation.turn).toBe('idle');
    expect(controller.state().alert).toEqual({ tone: 'error', text: '发送失败：连接已断开' });
    expect(transcript(controller).filter(entry => entry.kind === 'user')).toEqual([{ kind: 'user', text: '改标题' }]);
  });

  it('sends the failed message again on retry, offered only while the failure is the last thing that happened', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    await controller.send('改成蓝色');
    emit('chat.error', { error: 'error code: 429', code: 'rate_limited', upstream: true });
    expect(controller.state().retry).toBe(controller.state().conversation.entries.at(-1)!.id);

    await controller.retry();
    expect(gateway.sent('chat.send').map(params => params.content)).toEqual(['改成蓝色', '改成蓝色']);
    expect(controller.state().retry).toBeUndefined();
    emit('chat.tool_call', { tool_call: { id: 'w1', name: 'presentation_apply_patch' } });
    emit('chat.tool_result', { tool_result: { tool_call_id: 'w1', status: 'success' } });
    emit('chat.error', { error: 'error code: 429', code: 'rate_limited', upstream: true });
    expect(controller.state().retry).toBeUndefined();
  });

  it('runs the turn\'s PowerPoint call and stays running until the turn ends', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    await controller.send('看看结构');
    emit('chat.client_tool_request', {
      request_id: 'c1', interrupt_id: 'i1', tool_name: 'read_presentation_context', arguments: {} });
    emit('chat.processing_status', { is_processing: false });
    await settle();

    expect(execute).toHaveBeenCalledTimes(1);
    expect(gateway.sent('chat.send').at(-1)).toMatchObject({ source: 'client_tool_interrupt' });
    expect(controller.state().conversation.turn).toBe('running');
    emit('chat.processing_status', { is_processing: true });
    emit('chat.processing_status', { is_processing: false });
    expect(controller.state().conversation.turn).toBe('idle');
  });

  it('runs the next message on the model the user picked', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    expect(controller.state()).toMatchObject({ models: ['m-active', 'm-other'], model: 'm-active' });

    controller.chooseModel('m-other');
    await controller.send('检查标题');
    expect(gateway.sent('chat.send')[0]).toMatchObject({ content: '检查标题', model_name: 'm-other' });
  });

  it('sends a waiting message now as a supplement of the running turn, on the picked model', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    controller.chooseModel('m-other');
    await controller.send('检查标题');
    await controller.send('只看前 3 页');
    await controller.sendNow(controller.state().queue[0].id);

    expect(controller.state().queue).toEqual([]);
    expect(gateway.sent('chat.interrupt')).toEqual([
      { session_id: 's1', intent: 'supplement', new_input: '只看前 3 页', model_name: 'm-other' }]);
  });

  it('keeps a waiting message while the turn waits on the user\'s answer', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    await controller.send('检查标题');
    await controller.send('只看前 3 页');
    emit('chat.ask_user_question', PERMISSION);
    await controller.sendNow(controller.state().queue[0].id);

    expect(gateway.sent('chat.interrupt')).toEqual([]);
    expect(controller.state().queue.map(item => item.text)).toEqual(['只看前 3 页']);
    expect(controller.state().conversation.prompt).toBeDefined();
  });

  it('drops the writes the turn still had queued when a message takes the turn over', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    let release: () => void = () => undefined;
    execute.mockImplementationOnce(() => new Promise(resolve => {
      release = () => resolve({ ok: true, result: {} });
    }));
    await controller.send('做三页');
    for (const id of ['w1', 'w2']) {
      emit('chat.client_tool_request', {
        request_id: id, interrupt_id: `i-${id}`, tool_name: 'presentation_apply_patch', arguments: {} });
    }
    await settle();
    await controller.send('先别做了，换个主题');
    await controller.sendNow(controller.state().queue[0].id);
    release();
    await settle();

    expect(execute.mock.calls.map(([call]) => call.callId)).toEqual(['w1']);
  });

  it('reads the models again when the server changes them, falling back to its active one', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    controller.chooseModel('m-other');
    gateway.answer('models.list', { active_model: 'm-new', models: [{ model_name: 'm-third' }] });
    gateway.emit('models.updated', {});
    await settle();

    expect(gateway.sent('models.list')).toHaveLength(2);
    expect(controller.state()).toMatchObject({ models: ['m-new', 'm-third'], model: 'm-new' });
  });

  it('shows the history again after a reconnect, and the turn as ended when the server no longer runs it', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    await controller.send('检查标题');
    emit('chat.tool_call', { tool_call: { id: 't1', name: 'read_presentation_context' } });
    // The server restarted mid-turn: the turn's history stops with no reply, and nothing ended it on this socket.
    gateway.answer('client.conversation.messages', { messages: [
      { role: 'user', content: '检查标题' },
      { event_type: 'chat.tool_call', tool_call: { id: 't1', name: 'read_presentation_context' } },
      { event_type: 'chat.llm_call_start', content: '' },
      { event_type: 'chat.request_completed', content: '', status: 'cancelled' }] });
    gateway.answer('session.get_metadata', { session_id: 's1', is_processing: false, runtime: {} });
    gateway.connection({ kind: 'down', attempt: 1, retryAt: 1000 });
    gateway.connection({ kind: 'open' });
    await settle();

    expect(gateway.sent('client.conversation.messages')).toHaveLength(2);
    expect(controller.state().conversation.turn).toBe('idle');
    expect(transcript(controller)).toEqual([
      { kind: 'user', text: '检查标题' }, { kind: 'tool', name: 'read_presentation_context', status: 'failed' },
      { kind: 'notice', tone: 'info', text: '连接中断期间，这一轮已结束。' }]);
  });

  it('keeps the transcript shown when the history cannot be read again after a reconnect', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    gateway.answer('client.conversation.messages', new GatewayRequestError('down', 'UNAVAILABLE'));
    gateway.connection({ kind: 'down', attempt: 1, retryAt: 1000 });
    gateway.connection({ kind: 'open' });
    await vi.advanceTimersByTimeAsync(2000);

    expect(gateway.sent('client.conversation.messages')).toHaveLength(3);
    expect(transcript(controller)).toEqual([{ kind: 'user', text: '改标题' }, { kind: 'assistant', text: '好了。' }]);
    expect(controller.state().alert).toEqual({ tone: 'error', text: '历史消息暂时无法加载：down' });
  });

  it('follows the connection: down with the next attempt, and open again', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    gateway.connection({ kind: 'down', attempt: 2, retryAt: 5000 });
    expect(controller.state().connection).toEqual({ kind: 'down', attempt: 2, retryAt: 5000 });
    gateway.connection({ kind: 'open' });
    expect(controller.state().connection).toEqual({ kind: 'open' });
  });

  it('stops the turn', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    await controller.send('检查标题');
    await controller.send('再统一字号');
    await controller.stop();

    expect(gateway.sent('chat.interrupt')).toEqual([{ session_id: 's1', intent: 'cancel' }]);
    expect(controller.state().conversation.turn).toBe('idle');
    expect(controller.state().queue).toEqual([]);
  });
});

describe('undoing a turn', () => {
  /** A turn that wrote the presentation, from the user's message to its end; the id of its changes card. */
  async function turnThatWrote(controller: TaskPaneController): Promise<string> {
    await controller.send('做三页');
    emit('chat.tool_call', { tool_call: { id: 'w1', name: 'presentation_apply_patch' } });
    emit('chat.tool_result', { tool_result: { tool_call_id: 'w1', status: 'success' } });
    emit('chat.processing_status', { is_processing: false });
    await settle();
    return controller.state().conversation.entries.at(-1)!.id;
  }

  it('offers the undo of a turn that wrote, once its snapshots are closed, and none while a turn runs', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    const card = await turnThatWrote(controller);

    expect(snapshots.closeTurn).toHaveBeenCalledTimes(1);
    expect(snapshots.undoable).toHaveBeenLastCalledWith(['w1']);
    expect(controller.state().undo.offers).toEqual(new Set([card]));
    await controller.send('再改一下');
    expect(controller.state().undo.offers.size).toBe(0);
  });

  it('undoes the turn quietly, and tells only the agent with the next message that slide ids changed', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    const card = await turnThatWrote(controller);
    const before = transcript(controller);
    snapshots.undoable.mockReturnValue(undefined);
    await controller.undo(card);

    expect(snapshots.undo).toHaveBeenCalledWith(['w1'], false);
    expect(transcript(controller)).toEqual(before);
    expect(controller.state().undo.offers.size).toBe(0);
    await controller.send('换个配色');
    expect(gateway.sent('chat.send').at(-1)).toMatchObject({ content:
      '（已撤销你此前的修改，恢复了第 2、3 页；这些页的 id 已变化，请先重新读取演示文稿。）\n换个配色' });
    expect(transcript(controller).at(-1)).toEqual({ kind: 'user', text: '换个配色' });
    await controller.send('再试一次');
    emit('chat.processing_status', { is_processing: false });
    await settle();
    expect(gateway.sent('chat.send').at(-1)).toMatchObject({ content: '再试一次' });
  });

  it('asks before it overwrites slides changed since, and undoes once the user confirms', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    const card = await turnThatWrote(controller);
    snapshots.undo.mockResolvedValueOnce({ kind: 'changed', slides: 1 });
    await controller.undo(card);

    expect(controller.state().undo.confirm).toBe(card);
    controller.cancelUndo();
    expect(controller.state().undo.confirm).toBeUndefined();
    await controller.undo(card, true);
    expect(snapshots.undo).toHaveBeenLastCalledWith(['w1'], true);
  });
});

describe('approvals', () => {
  it('answers the backend\'s approval card with the chosen option, as the web chat does', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    emit('chat.ask_user_question', PERMISSION);
    const [allow] = controller.state().conversation.prompt!.questions[0].options;

    await controller.answer([{ selected: [allow] }]);
    expect(gateway.sent('chat.send')).toEqual([{
      session_id: 's1', query: '', mode: 'agent.work.normal', work_mode: 'work', request_id: 'call_7',
      interrupt_id: 'int_7', source: 'permission_interrupt',
      answers: [{ question: '允许发送任务吗？', selected_options: ['allow_once'], card_id: 'call_7' }],
    }]);
    expect(controller.state().conversation.prompt).toBeUndefined();
  });

  it('keeps the card when its answer did not go out', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    emit('chat.ask_user_question', PERMISSION);
    const [allow] = controller.state().conversation.prompt!.questions[0].options;

    gateway.answer('chat.send', new GatewayRequestError('chat.send 请求超时'));
    await controller.answer([{ selected: [allow] }]);
    expect(controller.state().conversation.prompt?.requestId).toBe('call_7');
  });

  it('shows the card a reopened pane gets back with its attach', async () => {
    gateway.answer('client.attach', { attachment_id: 'a1', renew_every_seconds: 30, pending: [
      { event_type: 'chat.ask_user_question', session_id: 's1', ...PERMISSION }] });
    const controller = pane();
    await controller.open();
    await settle();

    expect(controller.state().conversation.prompt?.requestId).toBe('call_7');
  });
});

describe('the document\'s conversations', () => {
  it('switches to another conversation and shows its history', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    let release: (value: JsonObject) => void = () => undefined;
    gateway.answer('client.conversation.messages', () => new Promise<JsonObject>(resolve => { release = resolve; }));
    await controller.switchTo('s0' as SessionId);
    await settle();

    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s1', 's0']);
    expect(transcript(controller)).toEqual([]);
    release({ messages: [{ role: 'user', content: '旧的' }] });
    await settle();
    emit('chat.final', { content: '来自之前的会话' }, 's1');
    expect(transcript(controller)).toEqual([{ kind: 'user', text: '旧的' }]);
  });

  it('keeps a waiting message that failed to go out off a conversation switched to meanwhile', async () => {
    let fail: (error: Error) => void = () => undefined;
    gateway.answer('chat.send', {}, () => new Promise<JsonObject>((_, reject) => { fail = reject; }));
    const controller = pane();
    await controller.open();
    await settle();
    await controller.send('检查标题');
    await controller.send('只看前 3 页');
    emit('chat.processing_status', { is_processing: false });
    await settle();
    await controller.switchTo('s0' as SessionId);
    await settle();
    fail(new GatewayRequestError('连接已断开'));
    await settle();
    emit('chat.processing_status', { is_processing: true }, 's0');
    emit('chat.processing_status', { is_processing: false }, 's0');
    await settle();

    expect(controller.state().queue).toEqual([]);
    expect(gateway.sent('chat.send').map(params => params.session_id)).toEqual(['s1', 's1']);
  });

  it('starts a new conversation and lists it', async () => {
    gateway.answer('session.create', { session_id: 's2' });
    const controller = pane();
    await controller.open();
    await settle();
    gateway.answer('client.conversations', { conversations: [{ session_id: 's2', title: '', last_message_at: 300 }] });
    await controller.newConversation();
    await settle();

    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s1', 's2']);
    expect(controller.state().conversations.map(item => item.sessionId)).toEqual(['s2']);
    expect(transcript(controller)).toEqual([]);
  });

  it('stays in a conversation nobody has written in yet, however fast new conversation is clicked', async () => {
    gateway.answer('session.create', { session_id: 's2' });
    const controller = pane();
    await controller.open();
    await settle();
    gateway.answer('client.conversation.messages', () => new Promise<JsonObject>(() => undefined));
    await Promise.all([controller.newConversation(), controller.newConversation()]);
    await controller.newConversation();
    await settle();

    expect(gateway.sent('session.create')).toHaveLength(1);
    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s1', 's2']);
  });

  it('starts a new conversation from one whose history could not be read', async () => {
    gateway.answer('client.conversation.messages', new GatewayRequestError('down', 'UNAVAILABLE'));
    gateway.answer('session.create', { session_id: 's2' });
    const controller = pane();
    await controller.open();
    await settle();
    await controller.newConversation();
    await settle();

    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s1', 's2']);
  });

  it('stops a turn that runs before it leaves it, and stays when the stop does not go out', async () => {
    gateway.answer('session.create', { session_id: 's2' });
    gateway.answer('chat.interrupt', new GatewayRequestError('busy', 'UNAVAILABLE'), {});
    const controller = pane();
    await controller.open();
    await settle();
    await controller.send('检查标题');
    await controller.switchTo('s0' as SessionId);

    expect(controller.state().alert).toEqual({ tone: 'error', text: '停止任务失败：busy' });
    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s1']);
    await controller.newConversation();
    await settle();
    expect(gateway.sent('chat.interrupt')).toEqual([
      { session_id: 's1', intent: 'cancel' }, { session_id: 's1', intent: 'cancel' }]);
    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s1', 's2']);
  });
});

describe('another window', () => {
  it('keeps the transcript when displaced, and takes the document back on "continue here"', async () => {
    gateway.answer('client.renew', new GatewayRequestError('displaced', 'DISPLACED'));
    const controller = pane();
    await controller.open();
    await settle();
    await vi.advanceTimersByTimeAsync(30_000);

    expect(controller.state().attachment.kind).toBe('displaced');
    expect(transcript(controller)).toHaveLength(2);
    expect(await controller.send('还能发吗')).toBe(false);
    expect(gateway.sent('chat.send')).toEqual([]);

    await controller.continueHere();
    expect(controller.state().attachment.kind).toBe('attached');
    expect(gateway.sent('client.attach').map(params => params.session_id)).toEqual(['s1', 's1']);
    expect(gateway.sent('client.conversation.messages')).toHaveLength(2);
  });
});
