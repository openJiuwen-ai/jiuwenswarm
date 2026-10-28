import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

// Reserved DOM origin only. HTTP is rejected; WebSocket is an in-memory transport, never a network client.
const dom = new JSDOM('<div id="root"></div>', { url: 'https://permission-answer.invalid', pretendToBeVisual: true });
class MemoryWebSocket {
  static OPEN = 1;
  static instance;
  constructor(url) {
    assert.equal(new URL(url).hostname, 'permission-answer.invalid');
    this.readyState = 0;
    this.requests = [];
    this.listeners = new Map();
    MemoryWebSocket.instance = this;
    queueMicrotask(() => { this.readyState = 1; this.onopen?.(); });
  }
  addEventListener(name, handler) {
    this.listeners.set(name, [...(this.listeners.get(name) ?? []), handler]);
  }
  send(raw) { this.requests.push(JSON.parse(raw)); }
  receive(frame) { this.onmessage?.({ data: JSON.stringify(frame) }); }
  close(code = 1000, reason = '') {
    this.readyState = 3;
    const event = { code, reason, wasClean: true };
    this.onclose?.(event);
    for (const handler of this.listeners.get('close') ?? []) handler(event);
  }
}
for (const [key, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  localStorage: dom.window.localStorage, HTMLElement: dom.window.HTMLElement, Node: dom.window.Node,
  Event: dom.window.Event, CustomEvent: dom.window.CustomEvent, IS_REACT_ACT_ENVIRONMENT: true,
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  fetch: () => { throw new Error('Unexpected HTTP request'); }, WebSocket: MemoryWebSocket,
})) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
after(() => dom.window.close());

const { useWebSocket } = await import('../node_modules/.cache/permission-answer-transport/hooks/useWebSocket.js');
const { useChatStore, useSessionStore } = await import('../node_modules/.cache/permission-answer-transport/stores/index.js');
const { default: i18n } = await import('../node_modules/.cache/permission-answer-transport/i18n/index.js');
const { webClient } = await import('../node_modules/.cache/permission-answer-transport/services/webClient.js');
const { setMultiModelCollaborationEnabled } = await import('../node_modules/.cache/permission-answer-transport/features/multiModelCollaboration/featureConfig.js');
const sessionId = 'attachment-transport';
async function mounted(run) {
  setMultiModelCollaborationEnabled(false);
  useChatStore.getState().ensureRuntime(sessionId);
  useChatStore.getState().setActiveSessionId(sessionId);
  useSessionStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().setMode(sessionId, 'agent');
  const observed = { errors: [] };
  function Host() {
    observed.api = useWebSocket({ activeSessionId: sessionId, onError: error => observed.errors.push(error) });
    return null;
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(Host))));
    await run({ observed, socket: MemoryWebSocket.instance });
  } finally {
    await act(async () => root.unmount());
    await webClient.disconnect();
    useChatStore.getState().removeRuntime(sessionId);
    useSessionStore.getState().removeRuntime(sessionId);
  }
}
const item = (type, index, extra = {}) => ({
  type, filename: `${index}.${type === 'image' ? 'png' : 'txt'}`,
  mimeType: type === 'image' ? 'image/png' : 'text/plain', base64Data: 'eA==', sizeBytes: 1, ...extra,
});
const respond = async (socket, request, payload) => act(async () => {
  socket.receive({ type: 'res', id: request.id, ok: true, payload });
});
function persisted(request) {
  const items = request.params.documents ?? request.params.media_items;
  // The actual backend only accepts eight items per persist call.
  return { media_items: items.slice(0, 8).map(source => ({
    type: request.method === 'document.persist' ? 'document' : 'image',
    filename: source.filename, mime_type: source.mime_type ?? source.mimeType,
    path: `/uploads/${source.filename}`, size_bytes: source.size_bytes ?? source.sizeBytes,
  })) };
}
for (const kinds of [Array(20).fill('image'), Array(20).fill('document'), ['image', ...Array(19).fill('document')]]) {
  test(`new session preserves ${kinds.filter(k => k === 'image').length} images and ${kinds.filter(k => k === 'document').length} documents`, async () => mounted(async ({ observed, socket }) => {
    const items = kinds.map((kind, i) => item(kind, i));
    let result;
    await act(async () => { result = observed.api.sendMessage('inspect {{skill:review}}', sessionId, items); });
    for (let index = 0; index < items.length; index += 1) {
      assert.equal(socket.requests.length, index + 1, 'only one upload is in flight');
      const request = socket.requests.at(-1);
      assert.equal((request.params.documents ?? request.params.media_items).length, 1);
      await respond(socket, request, persisted(request));
    }
    const send = socket.requests.at(-1);
    assert.equal(send.method, 'chat.send');
    assert.deepEqual(send.params.media_items.map(i => i.filename), items.map(i => i.filename));
    assert.equal((send.params.files.uploaded_images?.length ?? 0) + (send.params.files.uploaded_documents?.length ?? 0), 20);
    assert.ok(send.params.media_items.every(i => i.path && !('base64Data' in i) && !('base64_data' in i)));
    for (const document of items.filter(i => i.type === 'document')) assert.ok(send.params.content.includes(`/uploads/${document.filename}`));
    assert.ok(send.params.content.startsWith('inspect review'));
    await respond(socket, send, { accepted: true });
    assert.equal(await result, true);
    assert.deepEqual(observed.errors, []);
  }));
}
test('ready local paths are reused while unpersisted content is uploaded', async () => mounted(async ({ observed, socket }) => {
  const items = [item('document', 1, { path: '/local/large.txt', base64Data: undefined, sizeBytes: 101 * 1024 ** 2 }), item('image', 2)];
  let result;
  await act(async () => { result = observed.api.sendMessage('inspect', sessionId, items); });
  const upload = socket.requests.at(-1);
  assert.equal(upload.method, 'media.persist');
  await respond(socket, upload, persisted(upload));
  const send = socket.requests.at(-1);
  assert.deepEqual(socket.requests.map(r => r.method), ['media.persist', 'session.assets.register', 'chat.send']);
  const assetRegistration = socket.requests.find(r => r.method === 'session.assets.register');
  await respond(socket, assetRegistration, { items: [] });
  assert.equal(send.params.media_items[0].path, '/local/large.txt');
  await respond(socket, send, { accepted: true });
  assert.equal(await result, true);
}));
test('a rejected upload stops the send instead of silently dropping the attachment', async () => mounted(async ({ observed, socket }) => {
  let result;
  await act(async () => { result = observed.api.sendMessage('inspect', sessionId, [item('document', 1), item('document', 2), item('document', 3)]); });
  await respond(socket, socket.requests.at(-1), persisted(socket.requests.at(-1)));
  await respond(socket, socket.requests.at(-1), { media_items: [], document_errors: [{ error: 'rejected' }] });
  assert.equal(await result, false);
  assert.equal(socket.requests.length, 2);
  assert.ok(socket.requests.every(r => r.method === 'document.persist'));
  assert.ok(observed.errors[0].includes('2.txt'));
}));

for (const enabled of [false, true]) {
  test(`ordinary chat.send carries its model and no MoA bool when feature is ${enabled}`, async () => mounted(async ({ observed, socket }) => {
    useSessionStore.getState().setAvailableModels([{ model_name: 'ordinary-model', is_default: true }], 'ordinary-model');
    useSessionStore.getState().setSelectedModelName(sessionId, 'ordinary-model');
    setMultiModelCollaborationEnabled(enabled);
    let result;
    await act(async () => { result = observed.api.sendMessage('hello', sessionId); });
    const send = socket.requests.at(-1);
    assert.equal(send.method, 'chat.send');
    assert.equal(send.params.moa_enabled, undefined);
    assert.equal(send.params.model_name, 'ordinary-model');
    await respond(socket, send, { accepted: true });
    assert.equal(await result, true);
  }));
}

const groupSelection = { type: 'model_group', id: 'mgp-stable' };
for (const intent of ['resume', 'supplement']) {
  test(`${intent} preserves the stable group after collaboration is turned off`, async () => mounted(async ({ observed, socket }) => {
    useSessionStore.getState().setSelectedModelSelection(sessionId, groupSelection);
    let result;
    await act(async () => { result = observed.api.interrupt(sessionId, intent, { newInput: 'continue' }); });
    const request = socket.requests.at(-1);
    assert.equal(request.method, 'chat.interrupt');
    assert.equal(request.params.moa_enabled, undefined);
    assert.deepEqual(request.params.model_selection, groupSelection);
    assert.equal(request.params.model_name, undefined);
    await respond(socket, request, { success: true });
    assert.equal(await result, true);
  }));
}

test('structured chat.send carries the selected model group', async () => mounted(async ({ observed, socket }) => {
  useSessionStore.getState().setSelectedModelSelection(sessionId, groupSelection);
  setMultiModelCollaborationEnabled(true);
  let result;
  await act(async () => { result = observed.api.sendStructuredChatContent([{ type: 'text', text: 'hello' }], sessionId); });
  const request = socket.requests.at(-1);
  assert.equal(request.method, 'chat.send');
  assert.equal(request.params.moa_enabled, undefined);
  assert.deepEqual(request.params.model_selection, groupSelection);
  assert.equal(request.params.model_name, undefined);
  await respond(socket, request, { accepted: true });
  await result;
}));

for (const selection of [{ type: 'model', id: 'mdl-stable' }, groupSelection]) {
  for (const enabled of [false, true]) {
    test(`stable ${selection.type} chat.send with collaboration ${enabled}`, async () => mounted(async ({ observed, socket }) => {
      useSessionStore.getState().setSelectedModelSelection(sessionId, selection);
      setMultiModelCollaborationEnabled(enabled);
      let result;
      await act(async () => { result = observed.api.sendMessage('hello', sessionId); });
      if (selection.type === 'model_group' && !enabled) {
        assert.equal(await result, false);
        assert.equal(socket.requests.length, 0);
        assert.equal(observed.errors.length, 1);
        assert.deepEqual(useSessionStore.getState().getRuntime(sessionId).selectedModelSelection, selection);
        return;
      }
      const send = socket.requests.at(-1);
      assert.equal(send.method, 'chat.send');
      assert.equal(send.params.moa_enabled, undefined);
      assert.deepEqual(send.params.model_selection, selection);
      assert.equal(send.params.model_name, undefined);
      await respond(socket, send, { accepted: true });
      assert.equal(await result, true);
    }));
  }
}

test('disabling collaboration rejects an idle queued new message before claiming it', async () => mounted(async ({ observed, socket }) => {
  useSessionStore.getState().setSelectedModelSelection(sessionId, groupSelection);
  useChatStore.getState().addToTaskQueue(sessionId, 'queued-task');
  observed.api.drainTaskQueueIfIdle(sessionId);
  assert.equal(socket.requests.length, 0);
  assert.equal(useChatStore.getState().getRuntime(sessionId).taskQueue[0].status, 'queued');
  assert.equal(await observed.api.sendMessage('queued-task', sessionId, [], { queuedTaskId: 'stale-id' }), false);
  assert.equal(socket.requests.length, 0);
}));

test('a processing invocation can receive queued input after feature-off', async () => mounted(async ({ observed, socket }) => {
  useSessionStore.getState().setSelectedModelSelection(sessionId, groupSelection);
  useChatStore.getState().setProcessing(sessionId, true);
  useChatStore.getState().addToTaskQueue(sessionId, 'follow-up');
  const taskId = useChatStore.getState().getRuntime(sessionId).taskQueue.at(-1).id;
  let result;
  await act(async () => { result = observed.api.sendMessage('follow-up', sessionId, [], { queuedTaskId: taskId }); });
  const request = socket.requests.at(-1);
  assert.equal(request.method, 'chat.send');
  assert.equal(request.params.input_mode, 'steer');
  assert.deepEqual(request.params.model_selection, groupSelection);
  assert.equal(request.params.moa_enabled, undefined);
  await respond(socket, request, { accepted: true });
  await act(async () => socket.receive({ type: 'event', event: 'runtime.accepted', payload: { request_id: request.id, accepted: true } }));
  assert.equal(await result, true);
}));

for (const mode of ['agent', 'team']) {
  test(`${mode}: scheduled or team model groups remain available without collaboration`, async () => mounted(async ({ observed, socket }) => {
    useSessionStore.getState().setMode(sessionId, mode);
    useSessionStore.getState().setSelectedModelSelection(sessionId, groupSelection);
    if (mode === 'agent') useSessionStore.getState().setSessions([{ session_id: sessionId, cron_id: 'scheduled-job', mode }]);
    let result;
    await act(async () => { result = observed.api.sendMessage('hello', sessionId); });
    const request = socket.requests.at(-1);
    assert.equal(request.method, 'chat.send');
    assert.deepEqual(request.params.model_selection, groupSelection);
    await respond(socket, request, { accepted: true });
    assert.equal(await result, true);
    await act(async () => useSessionStore.getState().setSessions([]));
  }));
}


test('default ordinary model sends its stable ID even with a default model group after catalog loads', async () => mounted(async ({ observed, socket }) => {
  useSessionStore.getState().setAvailableModels([], 'ordinary-model');
  useSessionStore.getState().setSelectedModelName(sessionId, 'ordinary-model');
  useSessionStore.getState().setAvailableModels([{ model_id: 'mdl-ordinary', model_name: 'ordinary-model', is_default: true }], 'ordinary-model');
  useSessionStore.getState().setAvailableModelGroups([{ model_group_id: 'mgp-default', is_default: true,
    routes: [{ model_id: 'mdl-ordinary', route_id: 'r-default' }] }]);
  setMultiModelCollaborationEnabled(true);
  let result;
  await act(async () => { result = observed.api.sendMessage('hello', sessionId); });
  const request = socket.requests.at(-1);
  assert.deepEqual(request.params.model_selection, { type: 'model', id: 'mdl-ordinary' });
  assert.equal(request.params.model_name, undefined);
  assert.equal(request.params.moa_enabled, undefined);
  await respond(socket, request, { accepted: true });
  assert.equal(await result, true);
}));

for (const mode of ['agent', 'team']) {
  test(`${mode}: model-name-only execution events preserve a selected model group`, async () => mounted(async ({ observed, socket }) => {
    useSessionStore.getState().setMode(sessionId, mode);
    useSessionStore.getState().setAvailableModels([
      { model_id: 'mdl-glm', model_name: 'glm-5.3', is_default: true },
      { model_id: 'mdl-deepseek', model_name: 'deepseek-v4.1-flash', is_default: false },
    ], 'glm-5.3');
    useSessionStore.getState().setSelectedModelSelection(sessionId, groupSelection);
    setMultiModelCollaborationEnabled(true);
    useChatStore.getState().addMessage(sessionId, { id: 'initial-query', role: 'user', content: 'before', timestamp: new Date().toISOString() });
    await act(async () => socket.receive({ type: 'event', event: 'chat.message_updated', payload: {
      session_id: sessionId, updates: { query: 'after', model_name: 'glm-5.3' },
    } }));
    assert.deepEqual(useSessionStore.getState().getRuntime(sessionId).selectedModelSelection, groupSelection);
    assert.equal(useChatStore.getState().getRuntime(sessionId).messages.at(-1).content, 'after');
    assert.deepEqual(useSessionStore.getState().getOutgoingModelParams(sessionId), { model_selection: groupSelection });
    assert.equal(socket.requests.length, 0, 'member resolution does not submit a new selection');
  }));
}

test('an explicit stable selection event replaces the group while a legacy ordinary-model rewrite still updates its model', async () => mounted(async ({ socket }) => {
  useSessionStore.getState().setAvailableModels([
    { model_id: 'mdl-glm', model_name: 'glm-5.3', is_default: true },
    { model_id: 'mdl-deepseek', model_name: 'deepseek-v4.1-flash', is_default: false },
  ], 'glm-5.3');
  useSessionStore.getState().setSelectedModelSelection(sessionId, groupSelection);
  await act(async () => socket.receive({ type: 'event', event: 'chat.message_updated', payload: {
    session_id: sessionId, updates: { model_selection: { type: 'model', id: 'mdl-glm' } },
  } }));
  assert.deepEqual(useSessionStore.getState().getRuntime(sessionId).selectedModelSelection, { type: 'model', id: 'mdl-glm' });
  await act(async () => socket.receive({ type: 'event', event: 'chat.message_updated', payload: {
    session_id: sessionId, updates: { model_name: 'deepseek-v4.1-flash' },
  } }));
  assert.deepEqual(useSessionStore.getState().getRuntime(sessionId).selectedModelSelection, { type: 'model', id: 'mdl-deepseek' });
}));
