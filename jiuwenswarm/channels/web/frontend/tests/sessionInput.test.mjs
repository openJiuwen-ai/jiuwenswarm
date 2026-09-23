import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { act, createElement, Fragment } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM, VirtualConsole } from 'jsdom';
import { A2UIProvider } from '@a2ui/react';

const frontendRoot = fileURLToPath(new URL('..', import.meta.url));
let useWebSocket, useChatStore, useSessionStore, useGoalStore, webClient, TaskQueue, ChatTimelineList;

before(async () => {
  const outdir = join(frontendRoot, 'node_modules/.cache/session-input');
  await build({
    entryPoints: {
      hook: 'src/hooks/useWebSocket.ts',
      chatStore: 'src/stores/chatStore.ts',
      sessionStore: 'src/stores/sessionStore.ts',
      goalStore: 'src/stores/goalStore.ts',
      webClient: 'src/services/webClient.ts',
      TaskQueue: 'queue-component',
    },
    absWorkingDir: frontendRoot,
    bundle: true,
    splitting: true,
    format: 'esm',
    platform: 'node',
    packages: 'external',
    define: { 'import.meta.env': '{"DEV":false}', 'import.meta.glob': '__queueAssetGlob' },
    banner: { js: 'const __queueAssetGlob = () => ({});' },
    loader: { '.svg': 'dataurl', '.css': 'empty', '.png': 'dataurl', '.webp': 'dataurl' },
    plugins: [{
      name: 'queue-test-entry-and-assets',
      setup(builder) {
        builder.onResolve({ filter: /^queue-component$/ }, () => ({ path: 'queue-component', namespace: 'queue-entry' }));
        builder.onLoad({ filter: /.*/, namespace: 'queue-entry' }, () => ({
          contents: 'export { AgentActivityCard as TaskQueue } from "./src/components/ChatPanel/index.tsx"; export { ChatTimelineList } from "./src/components/ChatPanel/MessageList.tsx";',
          resolveDir: frontendRoot,
        }));
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-react-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
          contents: 'export default function SvgStub() { return null; }',
          loader: 'js',
        }));
        builder.onResolve({ filter: /^\/logo\.svg$/ }, () => ({ path: 'logo.svg', namespace: 'asset-url-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'asset-url-stub' }, () => ({
          contents: 'export default "logo.svg";',
          loader: 'js',
        }));
      },
    }],
    outdir,
  });
  const load = (name) => import(pathToFileURL(join(outdir, `${name}.js`)).href);
  ({ useWebSocket } = await load('hook'));
  ({ useChatStore } = await load('chatStore'));
  ({ useSessionStore } = await load('sessionStore'));
  ({ useGoalStore } = await load('goalStore'));
  ({ webClient } = await load('webClient'));
  ({ TaskQueue, ChatTimelineList } = await load('TaskQueue'));
});

async function mount(context) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/', virtualConsole: new VirtualConsole() });
  const sockets = [];
  class Socket {
    static OPEN = 1;
    readyState = 0;
    requests = [];
    listeners = [];
    constructor() {
      sockets.push(this);
      queueMicrotask(() => {
        this.readyState = 1;
        this.onopen?.();
      });
    }
    send(raw) {
      const request = JSON.parse(raw);
      this.requests.push(request);
      if (request.method === 'tts.synthesize') queueMicrotask(() => this.response(request.id));
    }
    receive(event, payload) {
      this.onmessage({ data: JSON.stringify({ type: 'event', event, payload }) });
    }
    response(id, ok = true, extra = {}) {
      this.onmessage({ data: JSON.stringify({ type: 'res', id, ok, payload: { accepted: ok }, ...extra }) });
    }
    addEventListener(name, callback) {
      if (name === 'close') this.listeners.push(callback);
    }
    close(code = 1000, reason = '') {
      this.readyState = 3;
      const event = { code, reason, wasClean: true };
      this.onclose?.(event);
      this.listeners.splice(0).forEach((callback) => callback(event));
    }
  }
  const globals = {
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    localStorage: dom.window.localStorage,
    CustomEvent: dom.window.CustomEvent,
    Event: dom.window.Event,
    WebSocket: Socket,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const previous = new Map();
  for (const [key, value] of Object.entries(globals)) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  context.mock.timers.enable({ apis: ['Date', 'setTimeout', 'setInterval'], now: 1800000000000 });
  const sid = 'steering-session';
  const store = useChatStore.getState();
  store.ensureRuntime(sid);
  store.setActiveSessionId(sid);
  useSessionStore.getState().ensureRuntime(sid);
  useSessionStore.getState().setMode(sid, 'agent');
  useGoalStore.getState().ensureRuntime(sid);
  let api;
  function Probe() {
    api = useWebSocket({ activeSessionId: sid });
    const activeId = useChatStore((state) => state.activeSessionId);
    const messages = useChatStore((state) => state.runtimes[activeId]?.messages);
    return createElement(Fragment, null,
      createElement(TaskQueue, {
        isProcessing: true,
        onSendTask: (text, media, options) => api.sendMessage(text, sid, media, options),
        onDrainTaskQueueIfIdle: api.drainTaskQueueIfIdle,
      }),
      createElement(A2UIProvider, null,
        createElement(ChatTimelineList, { messages: messages ?? [], sessionId: activeId, virtualized: false }),
      ),
    );
  }
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(createElement(Probe)));
  const socket = sockets[0];
  const runtime = () => useChatStore.getState().getRuntime(sid);
  const receive = (event, payload = {}) => act(() => socket.receive(event, { session_id: sid, ...payload }));
  receive('chat.processing_status', { is_processing: true, request_id: 'original' });
  receive('chat.delta', { content: 'original answer', request_id: 'original', execution_id: 'execution-A' });
  act(() => context.mock.timers.tick(16));
  const find = (taskId, action) =>
    document.querySelector(`[data-variant="${taskId}"] [data-testid="chat-panel-task-queue-item-${action}"]`);
  return {
    sid,
    store,
    runtime,
    receive,
    socket,
    find,
    receipt: (taskId) => runtime().taskInputReceipts[taskId],
    api: () => api,
    requests: () => socket.requests.filter((request) => request.method === 'chat.send'),
    queue(text, media) {
      act(() => store.addToTaskQueue(sid, text, media));
      return runtime().taskQueue.at(-1).id;
    },
    async click(id, action) {
      const button = find(id, action);
      assert.ok(button, `missing ${action}`);
      await act(async () => button.click());
    },
    async flush() {
      await act(async () => {});
    },
    async tick(ms) {
      await act(async () => context.mock.timers.tick(ms));
    },
    async dispose() {
      await act(async () => root.unmount());
      await webClient.disconnect();
      store.removeRuntime(sid);
      useSessionStore.getState().removeRuntime(sid);
      useGoalStore.getState().removeRuntime(sid);
      dom.window.close();
      context.mock.timers.reset();
      for (const [key, descriptor] of previous) {
        if (descriptor) Object.defineProperty(globalThis, key, descriptor);
        else delete globalThis[key];
      }
    },
  };
}

function addOriginalUser(c, content = 'original question') {
  act(() => c.store.addMessage(c.sid, {
    id: `original-user-${c.sid}`,
    role: 'user',
    content,
    timestamp: new Date(Date.now() - 1_000).toISOString(),
  }));
}

function assertNodeBefore(earlier, later, message) {
  const relation = earlier.compareDocumentPosition(later);
  const following = earlier.ownerDocument.defaultView.Node.DOCUMENT_POSITION_FOLLOWING;
  assert.ok(relation & following, message);
}

test('two queued messages: only the selected item steers, locks double click, and waits for Runtime ACK', async (context) => {
  const c = await mount(context);
  try {
    addOriginalUser(c);
    const first = c.queue('next task');
    const second = c.queue('extra constraint');
    assert.equal(c.requests().length, 0);
    const streamId = c.runtime().currentStreamId;
    const messages = c.runtime().messages;
    const button = c.find(second, 'send');
    assert.ok(button.classList.contains('chat-input-task-action--send'));
    assert.ok(button.querySelector('img'), 'the original send icon is retained');
    assert.equal(button.textContent.trim(), '', 'no additional text button');
    const originalMarkup = button.closest('[data-variant]').outerHTML;
    await c.click(second, 'send');
    assert.equal(c.find(second, 'send'), button, 'the existing button remains mounted');
    assert.equal(button.disabled, false, 'the original button appearance is unchanged');
    assert.equal(button.closest('[data-variant]').outerHTML, originalMarkup, 'sending adds no UI state or controls');
    await c.click(second, 'send');
    await act(async () => {
      void c.api().sendMessage('', c.sid, [], { queuedTaskId: second });
    });
    assert.equal(c.requests().length, 1);
    const request = c.requests()[0];
    assert.equal(request.params.content, 'extra constraint');
    assert.equal(request.params.input_mode, 'steer');
    assert.equal(request.params.expected_execution_id, 'execution-A');
    assert.equal(request.params.source, undefined, 'never uses permission/ask-user resume');
    assert.equal(c.runtime().taskQueue[1].status, 'sending');
    assert.equal(c.receipt(second).status, 'sending');
    assert.equal(c.find(second, 'delete').disabled, false);
    act(() => c.socket.response(request.id));
    await c.flush();
    assert.equal(c.runtime().taskQueue[1].status, 'sending', 'Gateway receipt is not Runtime acceptance');
    assert.equal(c.runtime().messages.filter((message) => message.supplementalInput).length, 0);
    assert.notEqual(c.receipt(second).status, 'accepted');
    c.receive('runtime.accepted', { request_id: 'unrelated' });
    assert.equal(c.runtime().taskQueue[1].status, 'sending');
    c.receive('runtime.accepted', { request_id: request.id });
    await c.flush();
    assert.deepEqual(
      c.runtime().taskQueue.map((item) => [item.id, item.status]),
      [
        [first, 'queued'],
      ],
    );
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(c.runtime().currentStreamId, streamId);
    assert.deepEqual(c.runtime().messages.slice(0, messages.length), messages, 'existing output is preserved');
    const bubble = c.runtime().messages.at(-1);
    assert.equal(bubble.role, 'user');
    assert.equal(bubble.content, 'extra constraint');
    assert.equal(bubble.supplementalInput.executionId, 'execution-A');
    c.receive('runtime.accepted', { request_id: request.id });
    assert.equal(c.runtime().messages.filter((message) => message.id === bubble.id).length, 1);
    assert.equal(c.find(second, 'send'), null, 'accepted input leaves the original queue');
    assert.equal(c.receipt(second).status, 'accepted');
    assert.equal(c.runtime().taskInputReceipts[second].requestId, request.id);
    c.receive('chat.delta', { content: ' continued', request_id: 'original' });
    await c.tick(16);
    assert.equal(c.runtime().messages.find((message) => message.id === streamId).content, 'original answer continued');
    const visible = [...document.querySelectorAll('[data-testid="chat-panel-message-bubble"]')].map((node) => node.textContent.trim());
    assert.deepEqual(visible, ['original question', 'original answer continued', 'extra constraint']);
    assert.equal(document.querySelectorAll('[data-testid="chat-panel-turn-elapsed-value"]').length, 2,
      'the supplemental bubble separates the two visible work sections of the running task');
    c.receive('chat.processing_status', { is_processing: false, request_id: 'original' });
    await c.flush();
    assert.equal(c.requests().length, 2);
    assert.equal(c.requests()[1].params.content, 'next task');
    act(() => c.socket.response(c.requests()[1].id));
  } finally {
    await c.dispose();
  }
});

test('the same queue send button starts an ordinary task when idle', async (context) => {
  const c = await mount(context);
  try {
    c.receive('chat.processing_status', { is_processing: false, request_id: 'original' });
    await c.flush();
    const first = c.queue('keep queued');
    const second = c.queue('send this ordinary task');
    await c.click(second, 'send');
    assert.equal(c.requests().length, 1);
    assert.equal(c.requests()[0].params.content, 'send this ordinary task');
    assert.equal(c.requests()[0].params.input_mode, 'steer');
    assert.equal(c.requests()[0].params.expected_execution_id, undefined);
    c.receive('runtime.accepted', { request_id: c.requests()[0].id, input_delivery: 'chat', execution_id: 'idle-chat' });
    await c.flush();
    assert.deepEqual(
      c.runtime().taskQueue.map((item) => item.id),
      [first],
    );
    assert.ok(c.find(first, 'send'), 'the remaining item uses the same button while busy');
    act(() => c.socket.response(c.requests()[0].id));
  } finally {
    await c.dispose();
  }
});

test('stale idle UI still sends supplemental intent and lets the backend steer its running task', async (context) => {
  const c = await mount(context);
  try {
    c.receive('chat.processing_status', { is_processing: false, request_id: 'original' });
    const id = c.queue('supplement backend task');
    c.queue('remain queued');
    await c.click(id, 'send');
    const req = c.requests()[0];
    assert.equal(req.params.input_mode, 'steer');
    assert.equal(req.params.expected_execution_id, undefined);
    c.receive('runtime.accepted', { request_id: req.id, execution_id: 'backend-running' });
    await c.flush();
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(c.runtime().activeExecutionId, 'backend-running');
    assert.equal(c.requests().length, 1);
    c.receive('chat.delta', { request_id: 'backend-task', content: 'continued backend output', execution_id: 'backend-running' });
    await c.tick(16);
    assert.ok(c.runtime().messages.some((msg) => msg.content.includes('continued backend output')));
    assert.equal(c.runtime().taskQueue.length, 1);
  } finally {
    await c.dispose();
  }
});

test('idle button rejection clears only its own pending state and leaves the item for manual retry', async (context) => {
  const c = await mount(context);
  try {
    c.receive('chat.processing_status', { is_processing: false, request_id: 'original' });
    const id = c.queue('rejected input');
    await c.click(id, 'send');
    c.receive('chat.error', { request_id: c.requests()[0].id, code: 'SESSION_CLOSED', error: 'session closed' });
    await c.flush();
    assert.equal(c.runtime().isProcessing, false);
    assert.equal(c.runtime().taskQueue[0].status, 'failed');
    assert.equal(c.receipt(id).error, 'session closed');
    assert.ok(!c.runtime().messages.some((msg) => msg.id === `user-steer-${id}`));
  } finally {
    await c.dispose();
  }
});

test('the original final completes one task without duplicating text around the supplemental bubble', async (context) => {
  const c = await mount(context);
  try {
    addOriginalUser(c);
    const streamId = c.runtime().currentStreamId;
    const anchor = c.runtime().thinkingAnchorAt;
    const id = c.queue('space theme');
    await c.click(id, 'send');
    c.receive('runtime.accepted', { request_id: c.requests()[0].id });
    await c.flush();
    assert.equal(c.runtime().thinkingAnchorAt, anchor);
    c.receive('chat.delta', { request_id: 'original', content: ' in space' });
    await c.tick(1000);
    assert.equal(c.runtime().currentStreamId, streamId);
    assert.equal(c.runtime().activeExecutionId, 'execution-A');
    c.receive('chat.final', { request_id: 'original', content: 'original answer in space' });
    await c.flush();
    const visible = [...document.querySelectorAll('[data-testid="chat-panel-message-bubble"]')].map(node => node.textContent.trim());
    assert.deepEqual(visible, ['original question', 'original answer in space', 'space theme']);
    assert.equal(document.querySelectorAll('[data-testid="chat-panel-turn-elapsed-value"]').length, 1);
    assert.equal(c.runtime().isProcessing, false);
    assert.equal(c.runtime().currentStreamId, null);
    assert.equal(c.requests().length, 1);
  } finally {
    await c.dispose();
  }
});

test('Host rejection preserves original output, pending question and Goal, and requires manual retry', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('keep this body');
    await c.click(id, 'send');
    const request = c.requests()[0];
    act(() => {
      c.store.enqueuePendingQuestion(c.sid, { request_id: 'question', source: 'ask_user_interrupt', questions: [] });
      useGoalStore.getState().setPendingAction(c.sid, 'resume');
    });
    const questions = c.runtime().pendingQuestions;
    const streamId = c.runtime().currentStreamId;
    c.receive('chat.error', { request_id: request.id, error: 'session is waiting for an interaction answer' });
    await c.flush();
    assert.equal(c.runtime().taskQueue[0].status, 'failed');
    assert.equal(c.runtime().taskQueue[0].content, 'keep this body');
    assert.equal(c.runtime().messages.filter((message) => message.supplementalInput).length, 0);
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(c.runtime().currentStreamId, streamId);
    assert.equal(c.runtime().pendingQuestions, questions);
    assert.equal(useGoalStore.getState().getRuntime(c.sid).pendingAction, 'resume');
    assert.equal(c.runtime().executionError, null);
    assert.equal(c.runtime().interruptResult, null, 'request feedback does not overwrite interrupt feedback');
    assert.equal(c.receipt(id).status, 'failed');
    assert.equal(c.receipt(id).error, "session is waiting for an interaction answer");
    assert.equal(c.requests().length, 1);
    assert.equal(c.find(id, 'send').disabled, false);
    assert.equal(c.find(id, 'requeue'), null, 'no additional UI controls');
    await c.click(id, 'send');
    assert.equal(c.runtime().taskQueue[0].status, 'failed');
    assert.equal(c.requests().length, 1);
  } finally {
    await c.dispose();
  }
});

test('unknown delivery is retained and never drained/retried; late ACK only settles its own item', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('uncertain');
    await c.click(id, 'send');
    const request = c.requests()[0];
    c.receive('chat.error', {
      request_id: request.id,
      code: 'SESSION_INPUT_DELIVERY_UNKNOWN',
      error: 'delivery uncertain',
    });
    await c.flush();
    assert.equal(c.runtime().taskQueue[0].status, 'unknown');
    assert.equal(c.receipt(id).status, 'unknown');
    assert.equal(c.receipt(id).error, "delivery uncertain");
    assert.equal(c.runtime().taskInputReceipts[id].errorCode, 'SESSION_INPUT_DELIVERY_UNKNOWN');
    assert.equal(c.find(id, 'send').disabled, false);
    assert.equal(c.find(id, 'requeue'), null);
    await c.click(id, 'send');
    c.receive('chat.processing_status', { is_processing: false, request_id: 'original' });
    await c.flush();
    assert.equal(c.requests().length, 1);
    c.receive('runtime.accepted', { request_id: request.id });
    assert.equal(c.runtime().taskQueue.length, 0);
    assert.equal(c.runtime().isProcessing, false);
    assert.equal(c.receipt(id).status, 'accepted');
    assert.equal(c.receipt(id).error, undefined);
  } finally {
    await c.dispose();
  }
});

for (const failure of ['timeout', 'disconnect']) {
  test(`${failure} after Gateway receipt keeps an unknown item without automatic resend`, async (context) => {
    const c = await mount(context);
    try {
      const id = c.queue(failure);
      await c.click(id, 'send');
      act(() => c.socket.response(c.requests()[0].id));
      if (failure === 'timeout') await c.tick(15001);
      else await act(async () => webClient.disconnect());
      assert.equal(c.runtime().taskQueue[0].status, 'unknown');
      assert.equal(c.requests().length, 1);
      assert.equal(c.runtime().isProcessing, true);
    } finally {
      await c.dispose();
    }
  });
}

test('attachments and pending interactions stay queued without changing the existing buttons', async (context) => {
  const c = await mount(context);
  try {
    const media = [{ type: 'document', filename: 'notes.txt', path: '/notes.txt' }];
    const attachment = c.queue('read notes', media);
    assert.equal(c.find(attachment, 'send').disabled, false);
    await c.click(attachment, 'send');
    const text = c.queue('text only');
    act(() =>
      c.store.enqueuePendingQuestion(c.sid, {
        request_id: 'permission',
        source: 'permission_interrupt',
        questions: [{ header: 'Permission', question: 'Allow?', options: [{ label: 'Allow' }, { label: 'Deny' }] }],
      }),
    );
    assert.equal(c.find(text, 'send').disabled, false);
    await act(async () => {
      void c.api().sendMessage('', c.sid, [], { queuedTaskId: text });
    });
    assert.equal(c.requests().length, 0);
    assert.deepEqual(c.runtime().taskQueue[0].mediaItems, media);
    assert.ok(c.runtime().taskQueue.every((item) => item.status === 'queued'));
    assert.equal(c.runtime().interruptResult.success, false);
  } finally {
    await c.dispose();
  }
});

test('queue drain wins a race: the removed task cannot also be steered', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('ordinary only');
    c.receive('chat.processing_status', { is_processing: false, request_id: 'original' });
    await act(async () => {
      void c.api().sendMessage('', c.sid, [], { queuedTaskId: id });
    });
    assert.equal(c.requests().length, 1);
    assert.equal(c.requests()[0].params.input_mode, undefined);
    assert.equal(c.runtime().taskQueue.length, 0);
    act(() => c.socket.response(c.requests()[0].id));
  } finally {
    await c.dispose();
  }
});

test('steering wins a race: idle drain waits for receipt and then sends only the remaining task', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('supplement');
    c.queue('later task');
    await c.click(id, 'send');
    c.receive('chat.processing_status', { is_processing: false, request_id: 'original' });
    assert.equal(c.requests().length, 1);
    c.receive('runtime.accepted', { request_id: c.requests()[0].id });
    await c.flush();
    assert.equal(c.requests().length, 2);
    assert.equal(c.requests()[1].params.content, 'later task');
    act(() => c.socket.response(c.requests()[1].id));
  } finally {
    await c.dispose();
  }
});

test('legacy chat.final wrapping runtime.accepted cannot end the running answer or clear Goal loading', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('legacy receipt');
    await c.click(id, 'send');
    act(() => useGoalStore.getState().setPendingAction(c.sid, 'set'));
    const streamId = c.runtime().currentStreamId;
    c.receive('chat.final', { event_type: 'runtime.accepted', request_id: c.requests()[0].id });
    await c.flush();
    assert.equal(c.runtime().taskQueue.length, 0);
    assert.equal(c.runtime().currentStreamId, streamId);
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(useGoalStore.getState().getRuntime(c.sid).pendingAction, 'set');
  } finally {
    await c.dispose();
  }
});

test('supplemental ACK and stream termination preserve the original stream, tools and queued work', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('receipt only');
    c.queue('must wait for original');
    await c.click(id, 'send');
    const requestId = c.requests()[0].id;
    const streamId = c.runtime().currentStreamId;
    const tools = c.runtime().toolExecutions;
    const buffers = c.runtime().streamBuffers;
    c.receive('runtime.accepted', { request_id: requestId });
    c.receive('chat.final', { request_id: requestId, content: '' });
    c.receive('chat.processing_status', { request_id: requestId, is_processing: false });
    await c.flush();
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(c.runtime().currentStreamId, streamId);
    assert.equal(c.runtime().toolExecutions, tools);
    assert.equal(c.runtime().streamBuffers, buffers);
    assert.equal(c.runtime().activeExecutionId, 'execution-A');
    assert.equal(c.requests().length, 1, 'a receipt cannot dispatch the next queued task');
    c.receive('chat.delta', { request_id: 'original', content: ' after receipt' });
    await c.tick(16);
    assert.equal(c.runtime().messages.find((message) => message.id === c.runtime().currentStreamId).content, 'original answer after receipt');
    assert.equal(c.receipt(id).status, 'accepted');
  } finally {
    await c.dispose();
  }
});

test('empty supplemental termination before ACK does not imply acceptance or finish the original', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('still waiting for receipt');
    await c.click(id, 'send');
    const requestId = c.requests()[0].id;
    const streamId = c.runtime().currentStreamId;
    c.receive('chat.final', { request_id: requestId });
    c.receive('chat.processing_status', { request_id: requestId, is_processing: false });
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(c.runtime().currentStreamId, streamId);
    assert.equal(c.runtime().taskInputReceipts[id].status, 'sending');
    assert.equal(c.receipt(id).status, 'sending');
    c.receive('chat.error', { request_id: requestId, error: 'SDK rejected input', code: 'INPUT_REJECTED' });
    await c.flush();
    assert.equal(c.receipt(id).status, 'failed');
    assert.equal(c.receipt(id).error, "SDK rejected input");
    assert.equal(c.runtime().isProcessing, true);
  } finally {
    await c.dispose();
  }
});

test('multiple equal-text supplements retain distinct delivery states without overwriting interrupt feedback', async (context) => {
  const c = await mount(context);
  try {
    const first = c.queue('same input');
    const second = c.queue('same input');
    const interrupt = { intent: 'pause', success: false, message: 'existing pause result' };
    act(() => c.store.setInterruptResult(c.sid, interrupt));
    await c.click(first, 'send');
    await c.click(second, 'send');
    const [one, two] = c.requests();
    act(() => c.socket.response(two.id, false, { error: 'SDK delivery rejected <details>', code: 'DELIVERY_REJECTED' }));
    c.receive('runtime.accepted', { request_id: one.id });
    c.receive('runtime.accepted', { request_id: one.id });
    await c.flush();
    assert.equal(c.receipt(first).status, 'accepted');
    assert.equal(c.receipt(second).status, 'failed');
    assert.equal(c.receipt(second).error, "SDK delivery rejected <details>");
    assert.equal(document.querySelector('[data-testid="chat-panel-task-input-receipt"]'), null);
    assert.equal(c.runtime().taskInputReceipts[second].errorCode, 'DELIVERY_REJECTED');
    assert.equal(c.runtime().interruptResult, interrupt);
    assert.equal(Object.keys(c.runtime().taskInputReceipts).length, 2);
    await c.tick(3001);
    assert.ok(c.receipt(first), 'receipts remain associated after interrupt feedback expires');
    assert.ok(c.receipt(second));
    c.receive('chat.error', { request_id: one.id, error: 'late duplicate error' });
    assert.equal(c.receipt(first).status, 'accepted');
    assert.equal(c.runtime().isProcessing, true);
  } finally {
    await c.dispose();
  }
});

test('a connection failure before assigning a request ID stays associated with the selected message', async (context) => {
  const c = await mount(context);
  const originalRequest = webClient.request;
  try {
    webClient.request = async () => {
      throw Object.assign(new Error('Connection unavailable before send'), { code: 'WS_NOT_READY' });
    };
    const id = c.queue('not sent');
    await c.click(id, 'send');
    await c.flush();
    assert.equal(c.runtime().taskInputReceipts[id].requestId, undefined);
    assert.equal(c.runtime().taskInputReceipts[id].status, 'failed');
    assert.equal(c.receipt(id).status, 'failed');
    assert.equal(c.receipt(id).error, "Connection unavailable before send");
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(c.requests().length, 0);
  } finally {
    webClient.request = originalRequest;
    await c.dispose();
  }
});

test('accepted steering preserves the thinking display while recording a lazy reasoning boundary', async (context) => {
  const c = await mount(context);
  try {
    c.receive('chat.reasoning', { request_id: 'original', content: 'original reasoning only' });
    const header = () => document.querySelector('[data-testid="chat-panel-reasoning-panel-header"]').outerHTML;
    const originalHeader = header();
    const reasoning = c.runtime().reasoningSegments;
    const assertStableDisplay = () => {
      assert.equal(header(), originalHeader);
      assert.equal(document.querySelector('[data-testid="chat-panel-task-input-feedback"]'), null);
      assert.equal(document.querySelector('[data-testid="chat-panel-task-input-receipt"]'), null);
      assert.equal(document.querySelector('[data-testid="chat-panel-reasoning-panel-body"]').textContent, 'original reasoning only');
      assert.equal(c.runtime().reasoningSegments, reasoning);
      assert.equal(c.runtime().isProcessing, true);
      assert.equal(c.runtime().interruptResult, null);
    };
    const accepted = c.queue('supplement');
    await c.click(accepted, 'send');
    assertStableDisplay();
    c.receive('runtime.accepted', { request_id: c.requests()[0].id });
    await c.flush();
    assert.equal(c.receipt(accepted).status, 'accepted');
    assert.equal(c.runtime().reasoningSegments[0].closed, false);
    assert.equal(c.runtime().reasoningInputBoundaryPending, true);
    assertStableDisplay();
    const failed = c.queue('rejected supplement');
    await c.click(failed, 'send');
    c.receive('chat.error', { request_id: c.requests()[1].id, error: 'SDK rejected input' });
    await c.flush();
    assert.equal(c.receipt(failed).status, 'failed');
    assertStableDisplay();
    const unknown = c.queue('unconfirmed supplement');
    await c.click(unknown, 'send');
    c.receive('chat.error', { request_id: c.requests()[2].id, error: 'delivery uncertain', code: 'SESSION_INPUT_DELIVERY_UNKNOWN' });
    await c.flush();
    assert.equal(c.receipt(unknown).status, 'unknown');
    assertStableDisplay();
  } finally {
    await c.dispose();
  }
});

for (const ackFirst of [true, false]) {
  test(`ordered input boundary freezes old output with ${ackFirst ? 'early' : 'late'} ACK`, async (context) => {
    const c = await mount(context);
    try {
      addOriginalUser(c);
      let seq = 0;
      const emit = (event, phase, payload = {}) => c.receive(event, {
        request_id: 'original', output_phase_id: phase,
        output_order: { request_id: 'original', sequence: ++seq },
        timestamp: Date.now(), ...payload,
      });
      emit('chat.output_phase', 'phase-1', { applied_input_ids: [] });
      emit('chat.reasoning', 'phase-1', { content: 'first thought' });
      const task = c.queue('new instruction');
      await c.click(task, 'send');
      const requestId = c.requests()[0].id;
      if (ackFirst) c.receive('runtime.accepted', { request_id: requestId, input_boundary: 'stream' });
      // This delta is still batched when the user marker arrives. It must be
      // flushed into the original bubble BEFORE adding the new user message.
      emit('chat.delta', 'phase-1', { content: ' visible prefix' });
      emit('chat.input_received', 'phase-1', { input_request_id: requestId, content: 'new instruction' });
      if (!ackFirst) c.receive('runtime.accepted', { request_id: requestId, input_boundary: 'stream' });
      emit('chat.delta', 'phase-1', { content: ' HIDDEN OLD TAIL', output_suppressed: true });
      emit('chat.reasoning', 'phase-1', { content: 'HIDDEN OLD THOUGHT', output_suppressed: true });
      emit('chat.final', 'phase-1', { content: 'HIDDEN OLD FINAL', output_suppressed: true });
      assert.equal(c.runtime().isProcessing, true);
      assert.equal(c.runtime().currentStreamId, null);
      assert.deepEqual(c.runtime().messages.filter(m => m.role === 'assistant').map(m => m.content), ['original answer visible prefix']);
      const frozenAnswer = c.runtime().messages.find(m => m.role === 'assistant');
      const supplementalUser = c.runtime().messages.find(m => m.supplementalInput);
      assert.equal(frozenAnswer.completedAt, supplementalUser.timestamp);
      emit('chat.output_phase', 'phase-2', { applied_input_ids: [requestId] });
      emit('chat.reasoning', 'phase-2', { content: 'second thought' });
      emit('chat.delta', 'phase-2', { content: 'new answer' });
      // Late old final cannot replace/finish the new phase, even without a
      // suppression bit: the phase identity itself rejects it.
      emit('chat.final', 'phase-1', { content: 'late old final' });
      assert.equal(c.runtime().isProcessing, true);
      emit('chat.final', 'phase-2', { content: 'new answer' });
      await c.flush();
      assert.deepEqual(c.runtime().messages.filter(m => m.role === 'assistant').map(m => m.content), ['original answer visible prefix', 'new answer']);
      const visible = [...document.querySelectorAll('[data-testid="chat-panel-message-bubble"]')].map(n => n.textContent.trim());
      assert.deepEqual(visible, ['original question', 'original answer visible prefix', 'new instruction', 'new answer']);
      assert.equal(c.runtime().messages.filter(m => m.supplementalInput).length, 1);
      assert.equal(c.runtime().messages.find(m => m.supplementalInput).supplementalInput.requestId, requestId);
      assert.equal(c.runtime().outputPhaseId, 'phase-2');
      assert.equal(c.runtime().reasoningSegments.length, 2);
      assert.ok(c.runtime().reasoningSegments.every(s => s.closed));
      assert.equal(c.runtime().isProcessing, false);
    } finally {
      await c.dispose();
    }
  });
}

test('a late supplement rejection preserves the message without starting a new turn', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('fallback input');
    await c.click(id, 'send');
    const requestId = c.requests()[0].id;
    c.receive('chat.final', { request_id: 'original', content: 'original finished' });
    c.receive('chat.error', { request_id: requestId, error: 'the targeted execution has ended' });
    c.receive('chat.processing_status', { request_id: requestId, is_processing: false });
    await c.flush();
    const messages = c.runtime().messages;
    assert.equal(messages.filter((message) => message.role === 'user').length, 0);
    assert.ok(messages.some((message) => message.content === 'original finished'));
    assert.equal(c.runtime().taskQueue[0].status, 'failed');
    assert.equal(c.runtime().isProcessing, false);
    assert.equal(c.runtime().activeExecutionId, null);
    assert.equal(c.requests().length, 1, 'failed supplement is not automatically sent as a new task');
  } finally {
    await c.dispose();
  }
});

test('automatic Goal phases keep the same visible answer and reasoning segment', async (context) => {
  const c = await mount(context);
  try {
    addOriginalUser(c);
    c.receive('goal.updated', { goal: {
      goal_id: 'goal-phases', session_id: c.sid, objective: 'keep working',
      status: 'active', revision: 1, attempt_count: 1,
    } });
    c.receive('chat.output_phase', { output_phase_id: 'attempt-1', applied_input_ids: [] });
    c.receive('chat.reasoning', { output_phase_id: 'attempt-1', content: 'first thought. ' });
    const firstStreamId = c.runtime().currentStreamId;
    c.receive('chat.output_phase', { output_phase_id: 'attempt-2', applied_input_ids: [] });
    c.receive('chat.reasoning', { output_phase_id: 'attempt-2', content: 'second thought.' });
    c.receive('chat.delta', { request_id: 'original', output_phase_id: 'attempt-2', content: ' second attempt' });
    await c.tick(16);
    assert.equal(c.runtime().currentStreamId, firstStreamId);
    assert.deepEqual(c.runtime().messages.filter(m => m.role === 'assistant').map(m => m.content), [
      'original answer second attempt',
    ]);
    assert.equal(c.runtime().reasoningSegments.length, 1);
    assert.equal(c.runtime().reasoningSegments[0].text, 'first thought. second thought.');
    assert.equal(c.runtime().isProcessing, true);
  } finally {
    await c.dispose();
  }
});

test('Goal pause then clear ends processing without revealing the old steering tail', async (context) => {
  const c = await mount(context);
  try {
    addOriginalUser(c);
    const goal = (status) => c.receive('goal.updated', { goal: status ? {
      goal_id: 'goal-clear', session_id: c.sid, objective: 'keep working',
      status, revision: 1, attempt_count: 1,
    } : null });
    let sequence = 0;
    const emit = (event, payload = {}) => c.receive(event, {
      request_id: 'original', output_phase_id: 'attempt-1',
      output_order: { request_id: 'original', sequence: ++sequence },
      timestamp: Date.now(), ...payload,
    });
    goal('active');
    emit('chat.output_phase', { applied_input_ids: [] });
    const task = c.queue('new instruction');
    await c.click(task, 'send');
    const requestId = c.requests()[0].id;
    emit('chat.input_received', { input_request_id: requestId, content: 'new instruction' });
    c.receive('runtime.accepted', { request_id: requestId, input_boundary: 'stream' });
    emit('chat.delta', { content: 'HIDDEN OLD TAIL', output_suppressed: true });
    emit('chat.reasoning', { content: 'HIDDEN OLD THOUGHT', output_suppressed: true });
    emit('chat.final', { content: 'HIDDEN OLD FINAL', output_suppressed: true });
    assert.equal(c.runtime().isProcessing, true);
    goal('paused');
    goal(null);
    // The adapter's stream-end control is unsuppressed even though this input
    // was never consumed. Goal streams have no chat.processing_status=false.
    emit('chat.final', { content: '' });
    await c.flush();
    assert.equal(c.runtime().isProcessing, false);
    assert.equal(c.runtime().isThinking, false);
    assert.equal(c.runtime().currentStreamId, null);
    assert.equal(useGoalStore.getState().getRuntime(c.sid).goal, null);
    const visible = [...document.querySelectorAll('[data-testid="chat-panel-message-bubble"]')].map(n => n.textContent.trim());
    assert.deepEqual(visible, ['original question', 'original answer', 'new instruction']);
  } finally {
    await c.dispose();
  }
});

test('supplement events cannot end or replace a newer execution', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('fallback that fails');
    await c.click(id, 'send');
    const requestId = c.requests()[0].id;
    c.receive('chat.final', { request_id: 'original', content: 'original finished' });
    c.receive('chat.processing_status', { request_id: 'replacement', is_processing: true });
    c.receive('chat.reasoning', { request_id: 'replacement', execution_id: 'execution-B', content: 'new task' });
    c.receive('chat.error', { request_id: requestId, error: 'the targeted execution has changed' });
    c.receive('chat.final', { request_id: requestId, content: '' });
    c.receive('chat.processing_status', { request_id: requestId, is_processing: false });
    await c.flush();
    assert.equal(c.runtime().executionError, null);
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(c.runtime().activeExecutionId, 'execution-B');
    assert.equal(c.runtime().taskQueue[0].status, 'failed');
    assert.equal(c.requests()[0].params.expected_execution_id, 'execution-A');
  } finally {
    await c.dispose();
  }
});

test('without execution identity the backend accepts steering and the original stream stays active', async (context) => {
  const c = await mount(context);
  try {
    act(() => c.store.setActiveExecutionId(c.sid, ''));
    const streamId = c.runtime().currentStreamId;
    const id = c.queue('use the current backend task');
    await c.click(id, 'send');
    const req = c.requests()[0];
    assert.equal(req.method, 'chat.send');
    assert.equal(req.params.input_mode, 'steer');
    assert.equal(req.params.expected_execution_id, undefined);
    c.receive('runtime.accepted', { request_id: req.id });
    await c.flush();
    assert.equal(c.receipt(id).status, 'accepted');
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(c.runtime().currentStreamId, streamId);
    assert.ok(c.runtime().messages.find((msg) => msg.id === `user-steer-${id}`).supplementalInput);
  } finally {
    await c.dispose();
  }
});

for (const terminal of ['chat.final', 'chat.error']) {
  test(`backend idle admission turns an unbound input into ordinary chat: ${terminal}`, async (context) => {
    const c = await mount(context);
    try {
      act(() => c.store.setActiveExecutionId(c.sid, ''));
      const id = c.queue('unbound input');
      c.queue('next queued task');
      await c.click(id, 'send');
      const req = c.requests()[0];
      c.receive('chat.final', { request_id: 'original', content: 'old task completed' });
      c.receive('chat.processing_status', { request_id: 'original', is_processing: false });
      assert.equal(c.requests().length, 1, 'pending admission prevents ordinary draining');
      const ack = { request_id: req.id, input_delivery: 'chat', execution_id: 'new-chat' };
      c.receive('runtime.accepted', ack);
      c.receive('runtime.accepted', ack);
      await c.flush();
      assert.equal(c.runtime().isProcessing, true);
      assert.equal(c.runtime().activeExecutionId, 'new-chat');
      const users = c.runtime().messages.filter((msg) => msg.id === `user-steer-${id}`);
      assert.equal(users.length, 1);
      assert.equal(users[0].supplementalInput, undefined, 'idle fallback starts a normal user turn');
      assert.equal(c.requests().length, 1);
      c.receive('chat.delta', { request_id: req.id, content: 'new response', execution_id: 'new-chat' });
      await c.tick(16);
      assert.ok(c.runtime().messages.some((msg) => msg.content === 'new response'));
      c.receive(terminal, { request_id: req.id, content: 'new response', error: 'new chat failed' });
      c.receive('chat.processing_status', { request_id: req.id, is_processing: false });
      await c.flush();
      assert.equal(c.requests().length, 2, 'normal completion resumes the remaining queue');
      assert.equal(c.requests()[1].params.input_mode, undefined);
      act(() => c.socket.response(c.requests()[1].id));
    } finally {
      await c.dispose();
    }
  });
}

test('manual retry uses a new request, ignores the previous ACK, and retains the message', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('retry once');
    await c.click(id, 'send');
    const oldId = c.requests()[0].id;
    c.receive('chat.error', { request_id: oldId, error: 'not sent; task is finishing' });
    await c.flush();
    assert.equal(c.runtime().taskQueue[0].status, 'failed');
    await c.click(id, 'send');
    const newId = c.requests()[1].id;
    assert.notEqual(newId, oldId);
    c.receive('runtime.accepted', { request_id: oldId });
    assert.equal(c.runtime().taskQueue[0].status, 'sending');
    assert.equal(c.receipt(id).status, 'sending');
    c.receive('runtime.accepted', { request_id: newId });
    await c.flush();
    assert.equal(c.runtime().taskQueue.length, 0);
    assert.equal(c.runtime().taskInputRequests[newId].content, 'retry once');
    assert.equal(c.runtime().taskInputReceipts[id].requestId, newId);
    assert.equal(c.receipt(id).status, 'accepted');
  } finally {
    await c.dispose();
  }
});

test('switching sessions and dismissing a receipt do not route its late error into another conversation', async (context) => {
  const c = await mount(context);
  const other = 'other-session';
  try {
    const id = c.queue('belongs to first session');
    await c.click(id, 'send');
    const requestId = c.requests()[0].id;
    act(() => {
      c.store.ensureRuntime(other);
      c.store.setProcessing(other, true);
      c.store.setActiveSessionId(other);
    });
    c.receive('runtime.accepted', { request_id: requestId });
    await c.flush();
    assert.equal(c.runtime().taskQueue.length, 0);
    assert.deepEqual(c.store.getRuntime(other).taskInputReceipts, {});
    act(() => c.store.setActiveSessionId(c.sid));
    assert.equal(c.receipt(id).status, 'accepted');
    act(() => c.store.removeFromTaskQueue(c.sid, id));
    c.receive('chat.error', { request_id: requestId, error: 'late duplicate' });
    assert.equal(c.runtime().isProcessing, true);
    assert.equal(c.runtime().executionError, null);
    assert.equal(c.store.getRuntime(other).isProcessing, true);
    assert.equal(c.store.getRuntime(other).executionError, null);
    assert.deepEqual(c.store.getRuntime(other).taskQueue, []);
  } finally {
    act(() => c.store.removeRuntime(other));
    await c.dispose();
  }
});

test('clearing conversation feedback still isolates late receipt errors from execution', async (context) => {
  const c = await mount(context);
  try {
    const id = c.queue('clear this receipt');
    await c.click(id, 'send');
    const requestId = c.requests()[0].id;
    act(() => c.store.clearMessages(c.sid));
    c.receive('chat.error', { request_id: requestId, error: 'late error for cleared input' });
    await c.flush();
    assert.equal(c.receipt(id), undefined);
    assert.deepEqual(c.runtime().taskInputReceipts, {});
    assert.equal(c.runtime().executionError, null);
  } finally {
    await c.dispose();
  }
});

test('editing, deleting and clearing queued messages preserve sending and unknown delivery records', async (context) => {
  const c = await mount(context);
  try {
    const queued = c.queue('edit me');
    const sending = c.queue('in flight');
    await c.click(sending, 'send');
    await c.click(queued, 'edit');
    assert.equal(c.runtime().inputValue, 'edit me');
    c.queue('clear me');
    act(() => {
      c.store.removeFromTaskQueue(c.sid, sending);
      c.store.clearTaskQueue(c.sid);
    });
    assert.deepEqual(
      c.runtime().taskQueue.map((item) => item.id),
      [sending],
    );
    await c.tick(15001);
    act(() => c.store.clearTaskQueue(c.sid));
    assert.equal(c.runtime().taskQueue[0].status, 'unknown');
    await c.click(sending, 'delete');
    assert.equal(c.runtime().taskQueue.length, 0);
    c.receive('runtime.accepted', { request_id: c.requests()[0].id });
    assert.equal(c.receipt(sending).status, 'accepted');
  } finally {
    await c.dispose();
  }
});
