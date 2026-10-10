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
  CustomEvent: dom.window.CustomEvent, IS_REACT_ACT_ENVIRONMENT: true,
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  fetch: () => { throw new Error('Unexpected HTTP request'); }, WebSocket: MemoryWebSocket,
})) Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
after(() => dom.window.close());

const { useWebSocket } = await import('../node_modules/.cache/permission-answer-transport/hooks/useWebSocket.js');
const { AuthorizationPrompt } = await import('../node_modules/.cache/permission-answer-transport/components/InteractionSlot/AuthorizationPrompt.js');
const { useChatStore, useSessionStore } = await import('../node_modules/.cache/permission-answer-transport/stores/index.js');
const { default: i18n } = await import('../node_modules/.cache/permission-answer-transport/i18n/index.js');
const { webClient } = await import('../node_modules/.cache/permission-answer-transport/services/webClient.js');
const { evaluatePlanToggle } = await import('../node_modules/.cache/permission-answer-transport/features/planMode/planModeGate.js');
const sessionId = 'permission-transport';
const question = (extra = {}) => ({
  header: 'Read permission', question: 'Read protected file?', tool_payload: { file_path: '/protected.txt' },
  options: [{ value: 'allow_once', label: 'Allow once' }, { value: 'reject', label: 'Reject' }], ...extra,
});
const payload = (questions = [question()]) => ({ session_id: sessionId, request_id: 'sdk-request', source: 'permission_interrupt', questions });

async function mounted(run) {
  useChatStore.getState().ensureRuntime(sessionId);
  useChatStore.getState().setActiveSessionId(sessionId);
  useSessionStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().setMode(sessionId, 'agent');
  const observed = { results: [], errors: [] };
  function Host() {
    observed.api = useWebSocket({ activeSessionId: sessionId, onError: (error) => observed.errors.push(error) });
    const pending = useChatStore((state) => state.runtimes[sessionId]?.pendingQuestions[0]);
    return pending ? createElement(AuthorizationPrompt, {
      pending,
      onSubmit: async (...args) => {
        const result = await observed.api.sendUserAnswer(sessionId, ...args);
        observed.results.push(result);
        return result;
      },
    }) : null;
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(Host))));
    await run({ ...observed, observed, socket: MemoryWebSocket.instance });
  } finally {
    await act(async () => root.unmount());
    await webClient.disconnect();
    useChatStore.getState().removeRuntime(sessionId);
    useSessionStore.getState().removeRuntime(sessionId);
  }
}
const prompt = () => document.querySelector('[data-testid="interaction-slot-auth-prompt"]');
const deliver = async (socket, value) => act(async () => socket.receive({ type: 'event', event: 'chat.ask_user_question', payload: value }));
const respond = async (socket, request, ok = true) => act(async () => socket.receive({ type: 'res', id: request.id, ok, payload: { accepted: ok } }));
const runtimeAck = async (socket, id) => act(async () => socket.receive({ type: 'event', event: 'runtime.accepted', payload: { request_id: id, accepted: true } }));
const assertPlanToggleAllowed = (allowed) => {
  for (const next of [false, true]) assert.equal(evaluatePlanToggle(sessionId, next).ok, allowed);
};

for (const count of [1, 2]) for (const action of ['allow-once', 'reject']) {
  test(`legacy ${count}-question ${action}: actual event → prompt → hook → res consumes pending`, async () => mounted(async ({ socket, observed }) => {
    await deliver(socket, payload(Array.from({ length: count }, () => question())));
    assertPlanToggleAllowed(false);
    assert.ok(prompt(), 'legacy permission must be displayed');
    await act(async () => document.querySelector(`[data-variant="${action}"]`).click());
    const request = socket.requests.at(-1);
    assert.equal(request.method, 'chat.send');
    assert.equal(request.params.request_id, 'sdk-request');
    assert.deepEqual(request.params.answers, Array.from({ length: count }, () => ({ selected_options: [action === 'reject' ? 'reject' : 'allow_once'] })));
    assert.ok(prompt(), 'not consumed before acceptance');
    await respond(socket, request);
    assert.deepEqual(observed.results, [true]);
    assert.equal(prompt(), null);
    assertPlanToggleAllowed(true);
    assert.deepEqual(observed.errors, []);
  }));
}

test('Smart prompt remains pending after res and unrelated ack; exact runtime ack consumes it', async () => mounted(async ({ socket, observed }) => {
  await deliver(socket, payload([question({ card_id: 'exact-card' })]));
  assert.equal(prompt().dataset.cardIds, 'exact-card');
  await act(async () => document.querySelector('[data-variant="allow-once"]').click());
  const request = socket.requests.at(-1);
  assert.deepEqual(request.params.answers, [{ selected_options: ['allow_once'], card_id: 'exact-card' }]);
  await respond(socket, request);
  await runtimeAck(socket, 'foreign');
  assert.deepEqual(observed.results, []);
  assert.ok(prompt());
  await runtimeAck(socket, request.id);
  assert.deepEqual(observed.results, [true]);
  assert.equal(prompt(), null);
}));

test('legacy caller cards are stripped, and blind or foreign permission answers send nothing', async () => mounted(async ({ socket, observed }) => {
  const answers = [{ selected_options: ['allow_once'], card_id: 'forged' }];
  assert.equal(await observed.api.sendUserAnswer(sessionId, 'sdk-request', answers, 'permission_interrupt'), false);
  await deliver(socket, payload());
  assert.equal(await observed.api.sendUserAnswer(sessionId, 'foreign', answers, 'permission_interrupt'), false);
  assert.equal(socket.requests.length, 0);
  let result;
  await act(async () => { result = observed.api.sendUserAnswer(sessionId, 'sdk-request', answers, 'permission_interrupt'); });
  const request = socket.requests.at(-1);
  assert.deepEqual(request.params.answers, [{ selected_options: ['allow_once'] }]);
  await respond(socket, request);
  assert.equal(await result, true);
}));

test('invalid or mixed Smart frames never display a legacy prompt', async () => mounted(async ({ socket }) => {
  for (const questions of [[question({ card_id: null })], [question({ card_id: '' })], [question({ card_id: 1 })],
    [question({ card_id: 'x'.repeat(129) })], [question(), question({ card_id: 'smart' })],
    [question({ card_id: 'a' }), question({ card_id: 'b' })], []]) {
    await deliver(socket, payload(questions));
    assert.equal(prompt(), null);
    assert.deepEqual(useChatStore.getState().getRuntime(sessionId).pendingQuestions, []);
  }
  assert.equal(socket.requests.length, 0);
}));

test('plan toggle retains processing, paused and ordinary ask guards', async () => mounted(async () => {
  assertPlanToggleAllowed(true);
  for (const setter of ['setProcessing', 'setPaused']) {
    await act(async () => useChatStore.getState()[setter](sessionId, true));
    assertPlanToggleAllowed(false);
    await act(async () => useChatStore.getState()[setter](sessionId, false));
    assertPlanToggleAllowed(true);
  }
  const ordinary = { ...payload(), source: 'ask_user', request_id: 'ordinary-question' };
  await act(async () => useChatStore.getState().enqueuePendingQuestion(sessionId, ordinary));
  assertPlanToggleAllowed(false);
  await act(async () => useChatStore.getState().consumePendingQuestion(sessionId, ordinary));
  assertPlanToggleAllowed(true);
}));

test('plan toggle waits for both cards and preserves a failed answer', async () => mounted(async ({ socket, observed }) => {
  await deliver(socket, payload([question({ card_id: 'first-card' })]));
  await deliver(socket, { ...payload([question({ card_id: 'second-card' })]), request_id: 'second-request' });
  const queueLength = () => useChatStore.getState().getRuntime(sessionId).pendingQuestions.length;
  assert.equal(queueLength(), 2);
  assertPlanToggleAllowed(false);
  await act(async () => document.querySelector('[data-variant="allow-once"]').click());
  await respond(socket, socket.requests.at(-1), false);
  assert.deepEqual(observed.results, [false]);
  assert.equal(queueLength(), 2);
  assertPlanToggleAllowed(false);
  for (const remaining of [1, 0]) {
    await act(async () => document.querySelector('[data-variant="allow-once"]').click());
    const request = socket.requests.at(-1);
    await respond(socket, request);
    assertPlanToggleAllowed(false);
    await runtimeAck(socket, request.id);
    assert.equal(queueLength(), remaining);
    assertPlanToggleAllowed(remaining === 0);
  }
  assert.deepEqual(observed.results, [false, true, true]);
}));
