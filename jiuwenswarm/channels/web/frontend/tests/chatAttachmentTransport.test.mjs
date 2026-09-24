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
const sessionId = 'attachment-transport';
async function mounted(run) {
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
  assert.deepEqual(socket.requests.map(r => r.method), ['media.persist', 'chat.send']);
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
