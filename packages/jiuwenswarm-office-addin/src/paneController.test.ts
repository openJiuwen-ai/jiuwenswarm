import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ClientAttachment, type SessionId } from './clientAttachment';
import type { Entry } from './conversationModel';
import type { DocumentId } from './documentIdentity';
import { GatewayRequestError, type JsonObject } from './gatewayClient';
import { HostWriteQueue } from './hostWriteQueue';
import { TaskPaneController } from './paneController';
import { PowerPointToolDispatcher, type PresentationToolRuntime } from './powerPointToolDispatcher';
import { presentationAttachDeclaration } from './presentationContract';
import { PresentationSessionFacade } from './presentationSessionFacade';
import { ScriptedGateway } from './scriptedGateway';
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
let execute: ReturnType<typeof vi.fn<PresentationToolRuntime['execute']>>;
let snapshots: {
  closeTurn: ReturnType<typeof vi.fn<SlideSnapshots['closeTurn']>>;
  undoable: ReturnType<typeof vi.fn<SlideSnapshots['undoable']>>;
  undo: ReturnType<typeof vi.fn<SlideSnapshots['undo']>>;
};

/** The pane as the task pane wires it, against a scripted Gateway and a stand-in PowerPoint. */
function pane(): TaskPaneController {
  execute = vi.fn<PresentationToolRuntime['execute']>(async () => ({ ok: true, result: { slide_count: 3 } }));
  const runtime: PresentationToolRuntime = { tools: new Set(['read_presentation_context']), execute };
  const sessions = new PresentationSessionFacade(gateway);
  const attachment = new ClientAttachment({
    gateway,
    executor: new PowerPointToolDispatcher(runtime, new HostWriteQueue()),
    declaration: async () => presentationAttachDeclaration({
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
  return new TaskPaneController({ attachment, sessions, documentId: DOCUMENT, connection: gateway, snapshots });
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
  let attaches = 0;
  gateway.answer('client.attach', () => ({ attachment_id: `a${++attaches}`, renew_every_seconds: 30, pending: [] }));
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
    expect(controller.state().documents.get('s9')).toBe('市场分析.pptx');
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
  it('sends a message to the attached conversation, and a message during a turn goes on as a supplement', async () => {
    const controller = pane();
    await controller.open();
    await settle();

    expect(await controller.send(' 检查标题 ')).toBe(true);
    expect(gateway.sent('chat.send')).toEqual([expect.objectContaining({ session_id: 's1', content: '检查标题' })]);
    expect(controller.state().conversation.turn).toBe('running');

    expect(await controller.send('只看前 3 页')).toBe(true);
    expect(gateway.sent('chat.interrupt')).toEqual([
      { session_id: 's1', intent: 'supplement', new_input: '只看前 3 页', model_name: 'm-active' }]);
    expect(gateway.sent('chat.send')).toHaveLength(1);
    expect(transcript(controller).filter(entry => entry.kind === 'user')).toEqual(
      ['改标题', '检查标题', '只看前 3 页'].map(text => ({ kind: 'user', text })));
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

  it('carries the picked model on a message that joins a running turn', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    controller.chooseModel('m-other');
    await controller.send('检查标题');
    await controller.send('只看前 3 页');

    expect(gateway.sent('chat.interrupt')).toEqual([
      { session_id: 's1', intent: 'supplement', new_input: '只看前 3 页', model_name: 'm-other' }]);
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

  it('shows the history again when it attaches again after a reconnect, as the turn may have ended meanwhile', async () => {
    const controller = pane();
    await controller.open();
    await settle();
    await controller.send('检查标题');
    gateway.answer('client.conversation.messages', { messages: [
      { role: 'user', content: '检查标题' }, { role: 'assistant', content: '标题都检查过了。' }] });
    gateway.connection({ kind: 'down', attempt: 1, retryAt: 1000 });
    gateway.connection({ kind: 'open' });
    await settle();

    expect(gateway.sent('client.conversation.messages')).toHaveLength(2);
    expect(controller.state().conversation.turn).toBe('idle');
    expect(transcript(controller).at(-1)).toEqual({ kind: 'assistant', text: '标题都检查过了。' });
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
    await controller.stop();

    expect(gateway.sent('chat.interrupt')).toEqual([{ session_id: 's1', intent: 'cancel' }]);
    expect(controller.state().conversation.turn).toBe('idle');
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
    expect(gateway.sent('chat.interrupt').at(-1)).toMatchObject({ new_input: '再试一次' });
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
