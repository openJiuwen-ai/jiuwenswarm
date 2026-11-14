import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import vm from 'node:vm';
import test, { beforeEach, afterEach } from 'node:test';
import { build } from 'esbuild';
import ts from 'typescript';

// Allow the identical behavioral checks to run against the deployed release source.
const root = process.env.FRONTEND_TEST_ROOT || fileURLToPath(new URL('../', import.meta.url));
const infrastructure = {
  '../utils/env': 'export const getGatewayHttpBase = () => "/api/v1";',
  '../auth/manager/authSession': 'export const hasManagerSessionCredentials = () => false; export const getManagerAccessToken = () => null; export const managerAuthenticatedFetch = (...args) => fetch(...args);',
  '../auth/config': 'export const isLoginAuthSimulateEnabled = () => false;',
  '../i18n': 'export default { t: key => key };',
  './runtimeScope': 'export const buildRuntimeIdentityHeaders = id => ({"X-Request-ID": id});',
  './webClient': 'export const PAUSABLE_STREAM_EVENTS = new Set();',
};
const result = await build({
  absWorkingDir: root, entryPoints: ['src/services/webHttpClient.ts'], bundle: true, write: false,
  platform: 'node', format: 'esm', define: { 'import.meta.env': '{}' },
  nodePaths: [fileURLToPath(new URL('../node_modules', import.meta.url))],
  plugins: [{ name: 'browser-infrastructure', setup(builder) {
    builder.onResolve({ filter: /.*/ }, args => args.importer.endsWith('webHttpClient.ts') && args.path in infrastructure
      ? { path: args.path, namespace: 'stub' } : undefined);
    builder.onLoad({ filter: /.*/, namespace: 'stub' }, args => ({ contents: infrastructure[args.path], loader: 'js' }));
  } }],
});
const { WebHttpClient } = await import(`data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`);
const storeBundle = await build({
  absWorkingDir: root, entryPoints: ['src/stores/todoStore.ts'], bundle: true, write: false,
  platform: 'node', format: 'esm', nodePaths: [fileURLToPath(new URL('../node_modules', import.meta.url))],
});
const { useTodoStore } = await import(`data:text/javascript;base64,${Buffer.from(storeBundle.outputFiles[0].text).toString('base64')}`);
const hookText = await readFile(path.join(root, 'src/hooks/useWebSocket.ts'), 'utf8');
const ast = ts.createSourceFile('useWebSocket.ts', hookText, ts.ScriptTarget.Latest, true);
let callback;
function visit(node) {
  if (ts.isCallExpression(node) && node.expression.getText(ast) === 'webClient.on' && node.arguments[0]?.text === 'chat.interrupt_result') {
    callback = node.arguments[1].getText(ast);
  }
  ts.forEachChild(node, visit);
}
visit(ast);
assert.ok(callback);
const chatState = new Proxy({ getRuntime: () => ({ switchingMode: false }) }, { get: (target, key) => target[key] || (() => {}) });
const handler = vm.runInNewContext(ts.transpileModule(`(${callback})`, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText, {
  useTodoStore, useChatStore: { getState: () => chatState },
  resolveEventSessionId: payload => payload.session_id, shouldDropDuplicatedEvent: () => false,
  flushPendingStreamDelta: () => {}, shouldClearPermissionQuestionsForLifecycleEvent: () => false,
  closeActiveTeamLeaderMessages: () => {}, isEnterprise: () => true,
  useSessionStore: { getState: () => ({ getRuntime: () => ({ mode: 'agent' }) }) },
});
const originalTodos = [
  { id: 'one', content: 'Finished', status: 'completed' },
  { id: 'two', content: 'Running', status: 'in_progress' },
  { id: 'three', content: 'Waiting', status: 'pending' },
];
const cancelledTodos = originalTodos.map(todo => ({ ...todo, status: todo.status === 'completed' ? 'completed' : 'cancelled' }));
const statuses = sid => useTodoStore.getState().getRuntime(sid).todos.map(todo => todo.status);

let previousWindow;
let previousFetch;
beforeEach(() => {
  previousWindow = Object.getOwnPropertyDescriptor(globalThis, 'window');
  previousFetch = globalThis.fetch;
  globalThis.window = { __JIUWENSWARM_EDITION__: 'enterprise', setTimeout, clearTimeout };
  for (const sid of ['s1', 'unrelated']) {
    useTodoStore.getState().ensureRuntime(sid);
    useTodoStore.getState().setTodos(sid, originalTodos);
  }
});
afterEach(() => {
  if (previousWindow) Object.defineProperty(globalThis, 'window', previousWindow);
  else delete globalThis.window;
  globalThis.fetch = previousFetch;
});

async function exercise({ ending = 'error', success = true, todos = cancelledTodos, status = 200, missingEvent = false } = {}) {
  const client = new WebHttpClient();
  const events = [];
  const requests = [];
  client.on('chat.interrupt_result', event => { events.push(event); handler(event); });
  const previousFetch = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    requests.push({ url, body: JSON.parse(init.body), headers: init.headers });
    // The transport must supply session scope when this older response omits it.
    const payload = { ...(missingEvent ? {} : { event_type: 'chat.interrupt_result' }), intent: 'cancel', success, ...(todos === undefined ? {} : { todos }) };
    return new Response(JSON.stringify({ ok: status === 200, payload }), { status, headers: { 'content-type': 'application/json' } });
  };
  const controller = new AbortController();
  const stream = new ReadableStream({ start(s) {
    if (ending === 'normal') s.enqueue(new TextEncoder().encode('event: chat.processing_status\ndata: {"session_id":"s1","is_processing":false}\n\n'));
    if (ending === 'chat-error') s.enqueue(new TextEncoder().encode('event: chat.error\ndata: {"session_id":"s1","error":"model failed"}\n\n'));
    if (ending === 'error') s.error(new TypeError('network error'));
    else s.close();
  } });
  if (ending === 'abort') controller.abort();
  if (ending === 'superseded') client.sseSuperseded.add('r1');
  try {
    await client.pumpSse(new Response(stream), 'r1', 'sse', controller, 's1');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(client.getInflightCount(), 0);
    assert.deepEqual(statuses('unrelated'), ['completed', 'in_progress', 'pending']);
    return { events, requests };
  } finally { globalThis.fetch = previousFetch; }
}

for (const ending of ['error', 'eof', 'chat-error']) {
  test(`${ending}: one automatic cancel receipt settles Progress using server snapshot`, async () => {
    const { events, requests } = await exercise({ ending });
    assert.equal(requests.length, 1);
    assert.equal(requests[0].url, '/api/v1/chat/s1/actions/interrupt');
    assert.deepEqual(requests[0].body, { intent: 'cancel' });
    assert.equal(events.length, 1);
    assert.notEqual(events[0].request_id, 'r1');
    assert.equal(events[0].request_id, requests[0].headers['X-Request-ID']);
    assert.equal(events[0].payload.session_id, 's1');
    assert.deepEqual(statuses('s1'), ['completed', 'cancelled', 'cancelled']);
  });
}
for (const ending of ['normal', 'abort', 'superseded']) {
  test(`${ending}: no automatic cancel or Todo mutation`, async () => {
    const { events, requests } = await exercise({ ending });
    assert.equal(requests.length, 0);
    assert.equal(events.length, 0);
    assert.deepEqual(statuses('s1'), ['completed', 'in_progress', 'pending']);
  });
}
for (const options of [{ success: false }, { status: 500 }, { missingEvent: true }, { todos: null }]) {
  test(`unconfirmed/missing snapshot ${JSON.stringify(options)} does not fabricate cancellation`, async () => {
    await exercise(options);
    assert.deepEqual(statuses('s1'), ['completed', 'in_progress', 'pending']);
  });
}
test('an authoritative empty snapshot clears Progress', async () => {
  await exercise({ todos: [] });
  assert.deepEqual(statuses('s1'), []);
});
test('manual cancellation receipt follows the same Todo synchronization', async () => {
  const client = new WebHttpClient();
  client.state = 'ready';
  client.on('chat.interrupt_result', handler);
  globalThis.fetch = async () => new Response(JSON.stringify({ ok: true, payload: {
    event_type: 'chat.interrupt_result', intent: 'cancel', success: true, todos: cancelledTodos,
  } }));
  await client.request('chat.interrupt', { session_id: 's1', intent: 'cancel' });
  assert.deepEqual(statuses('s1'), ['completed', 'cancelled', 'cancelled']);
});

for (const [source, nextSession] of [
  ['automatic', 's1'], ['automatic', 'unrelated'], ['manual', 's1'], ['manual', 'unrelated'],
]) {
  for (const nextFinished of [false, true]) {
    test(`late ${source} cancel after ${nextSession} starts (finished=${nextFinished})`, async () => {
      const client = new WebHttpClient();
      client.state = 'ready';
      const events = [];
      client.on('chat.interrupt_result', event => { events.push(event); handler(event); });
      let resolveCancel;
      let nextStream;
      globalThis.fetch = async url => {
        if (url.endsWith('/actions/interrupt')) {
          return new Promise(resolve => { resolveCancel = resolve; });
        }
        return new Response(new ReadableStream({ start(s) { nextStream = s; } }), {
          headers: { 'content-type': 'text/event-stream' },
        });
      };
      let nextRequest;
      client.on('chat.processing_status', () => {
        nextRequest ??= client.request('chat.send', { session_id: nextSession, query: 'next turn' });
      });
      let manualRequest;
      if (source === 'automatic') {
        await client.pumpSse(new Response(new ReadableStream({ start(s) { s.close(); } })),
          'old-chat', 'sse', new AbortController(), 's1');
      } else {
        manualRequest = client.request('chat.interrupt', { session_id: 's1', intent: 'cancel' });
        await new Promise(resolve => setImmediate(resolve));
        nextRequest = client.request('chat.send', { session_id: nextSession, query: 'next turn' });
      }
      await nextRequest;
      if (nextFinished) {
        nextStream.enqueue(new TextEncoder().encode(`event: chat.processing_status\ndata: {"session_id":"${nextSession}","is_processing":false}\n\n`));
        nextStream.close();
        await new Promise(resolve => setImmediate(resolve));
      }
      resolveCancel(new Response(JSON.stringify({ ok: true, payload: {
        event_type: 'chat.interrupt_result', session_id: 's1', intent: 'cancel',
        success: true, todos: cancelledTodos,
      } })));
      await manualRequest;
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(events.length, nextSession === 's1' ? 0 : 1);
      assert.deepEqual(statuses('s1'), nextSession === 's1'
        ? ['completed', 'in_progress', 'pending'] : ['completed', 'cancelled', 'cancelled']);
      if (!nextFinished) assert.equal(client.sseInflight.size, 1);
      await client.disconnect();
      if (!nextFinished) nextStream.close();
      await new Promise(resolve => setImmediate(resolve));
    });
  }
}
