import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import test, { mock } from 'node:test';
import { build } from 'esbuild';
import ts from 'typescript';

// Bundle the real transport, stubbing only browser/auth infrastructure.
const root = fileURLToPath(new URL('../', import.meta.url));
const webClientText = await readFile(new URL('../src/services/webClient.ts', import.meta.url), 'utf8');
const pausableDeclaration = webClientText.match(/export const PAUSABLE_STREAM_EVENTS = new Set\(\[[\s\S]*?\]\);/g);
assert.equal(pausableDeclaration?.length, 1, 'one shared pause-event declaration is required');
const infrastructure = {
  '../utils/env': 'export const getGatewayHttpBase = () => "/api/v1";',
  '../auth/manager/authSession':
    'export const hasManagerSessionCredentials = () => false; export const managerAuthenticatedFetch = (...args) => fetch(...args);',
  '../i18n': 'export default { t: key => key };',
  './runtimeScope': 'export const buildRuntimeIdentityHeaders = () => ({});',
  './webClient': pausableDeclaration[0],
};
const bundle = await build({
  absWorkingDir: root,
  entryPoints: ['src/services/webHttpClient.ts'],
  bundle: true,
  write: false,
  platform: 'node',
  format: 'esm',
  define: { 'import.meta.env.VITE_JIUWENSWARM_EDITION': '"personal"' },
  plugins: [
    {
      name: 'browser-infrastructure',
      setup(builder) {
        builder.onResolve({ filter: /.*/ }, args => {
          if (args.importer.endsWith('webHttpClient.ts') && args.path in infrastructure) {
            return { path: args.path, namespace: 'stub' };
          }
        });
        builder.onLoad({ filter: /.*/, namespace: 'stub' }, args => ({ contents: infrastructure[args.path], loader: 'js' }));
      },
    },
  ],
});
const { WebHttpClient } = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`);

const eventNames = ['chat.error', 'chat.final', 'chat.processing_status', 'chat.delta', 'history.message'];
const frame = (name, payload = {}, rid = 'r1') => `id: ${rid}\nevent: ${name}\ndata: ${JSON.stringify({ session_id: 's1', ...payload })}\n\n`;
const error = frame('chat.error', { error: 'retry exhausted' });
const final = frame('chat.final', { content: '' });
const end = frame('chat.processing_status', { is_processing: false });

async function pump(chunks, { edition = 'enterprise', kind = 'sse', filter, onEvent, failRead = false, paused = false } = {}) {
  globalThis.window = { __JIUWENSWARM_EDITION__: edition };
  const client = new WebHttpClient();
  const abort = new AbortController();
  const events = [];
  const buffered = [];
  if (paused) client.setPauseBufferHook({ isActive: () => true, onBuffer: event => buffered.push(event) });
  if (filter) client.setStreamEventFilter(filter);
  for (const name of eventNames)
    client.on(name, event => {
      events.push(event);
      onEvent?.(event, client, abort);
    });
  let cancelled = false;
  let index = 0;
  const stream = new ReadableStream(
    {
      pull(controller) {
        if (index < chunks.length) controller.enqueue(new TextEncoder().encode(chunks[index++]));
        else if (failRead) controller.error(failRead instanceof Error ? failRead : new Error('connection lost'));
        else controller.close();
      },
      cancel() {
        cancelled = true;
      },
    },
    { highWaterMark: 0 },
  );
  // TypeScript private method remains callable in JS; exercise the actual reader loop.
  const warnings = [];
  const warningMock = mock.method(console, 'warn', (...args) => warnings.push(args));
  try {
    await client.pumpSse(new Response(stream), 'r1', kind, abort, 's1');
  } finally {
    warningMock.mock.restore();
  }
  assert.equal(client.getInflightCount(), 0);
  assert.equal(client.sseInflight.size, 0);
  return { events, cancelled, buffered, client, warnings };
}

for (const chunks of [[error + final + end], [error, final, end]]) {
  test(`enterprise consumes error, final and task end (${chunks.length} chunks)`, async () => {
    const { events, cancelled } = await pump(chunks);
    assert.deepEqual(
      events.map(e => e.event),
      ['chat.error', 'chat.final', 'chat.processing_status'],
    );
    assert.equal(events.at(-1).payload.source, undefined);
    assert.equal(cancelled, true);
  });
}

test('error EOF emits exactly one scoped fallback, including an unterminated final frame', async () => {
  for (const chunks of [[error], [error, final.trimEnd()], [error.trimEnd()]]) {
    const { events } = await pump(chunks);
    const statuses = events.filter(e => e.event === 'chat.processing_status');
    assert.equal(statuses.length, 1);
    assert.equal(statuses[0].request_id, 'r1');
    assert.deepEqual(statuses[0].payload, {
      session_id: 's1',
      request_id: 'r1',
      is_processing: false,
      source: 'http_error_eof',
    });
    assert.equal(events[0].payload.error, 'retry exhausted');
  }
});

test('normal EOF and real task end never synthesize an error fallback', async () => {
  for (const chunks of [[final], [end], [error + end.trimEnd()]]) {
    const { events } = await pump(chunks);
    assert.equal(
      events.some(e => e.payload.source === 'http_error_eof'),
      false,
    );
  }
});

test('personal error/final still terminate immediately', async () => {
  for (const first of [error, final]) {
    const { events } = await pump([first + end], { edition: 'personal' });
    assert.equal(events.length, 1);
  }
});

test('history errors cannot synthesize a chat task end', async () => {
  const { events } = await pump([error], { kind: 'history-stream' });
  assert.deepEqual(
    events.map(e => e.event),
    ['chat.error'],
  );
});

test('aborted and replaced streams never synthesize completion', async () => {
  for (const action of ['abort', 'replace']) {
    const { events, warnings } = await pump([error, end], {
      onEvent(event, client, abort) {
        if (event.event !== 'chat.error') return;
        if (action === 'abort') abort.abort();
        if (action === 'replace') client.abortSseInflight({ sessionId: 's1', exceptRequestId: 'r2' });
      },
    });
    assert.deepEqual(
      events.map(e => e.event),
      ['chat.error'],
    );
    assert.equal(warnings.length, 0);
  }
  const { events, warnings } = await pump([error], { failRead: new DOMException('cancelled', 'AbortError') });
  assert.deepEqual(
    events.map(e => e.event),
    ['chat.error'],
  );
  assert.equal(warnings.length, 0);
});

test('read failure without a preceding chat error does not fabricate completion', async () => {
  const { events, warnings } = await pump([final], { failRead: true });
  assert.deepEqual(
    events.map(e => e.event),
    ['chat.final'],
  );
  assert.equal(warnings.length, 1);
});

test('dispatch exceptions are logged and still settle errored streams without leaking inflight state', async () => {
  for (const name of ['chat.error', 'chat.processing_status']) {
    const { events, warnings } = await pump([error, end], {
      onEvent(event) {
        if (event.event === name) throw new Error('handler failed');
      },
    });
    assert.equal(events.filter(e => e.payload.source === 'http_error_eof').length, 1);
    assert.equal(warnings.length, name === 'chat.error' ? 1 : 2);
  }
});

test('paused processing=true stays suppressed; personal pause semantics are unchanged', async () => {
  for (const edition of ['enterprise', 'personal']) {
    const { buffered } = await pump([frame('chat.processing_status', { is_processing: true }), end], { paused: true, edition });
    assert.equal(buffered.length, edition === 'enterprise' ? 1 : 0);
  }
});

test('fallback passes through the existing request suppression filter', async () => {
  const { events } = await pump([error], { filter: e => e.request_id !== 'r1' });
  assert.deepEqual(events, []);
});

// Execute the actual hook callbacks in isolation, without mounting the app or copying their logic.
const hookText = await readFile(new URL('../src/hooks/useWebSocket.ts', import.meta.url), 'utf8');
const hookAst = ts.createSourceFile('useWebSocket.ts', hookText, ts.ScriptTarget.Latest, true);
function findNode(predicate, within = hookAst, label = 'hook node') {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(within);
  assert.equal(matches.length, 1, `${label}: expected exactly one matching node`);
  return matches[0];
}
function eventCallback(name) {
  return findNode(
    node => ts.isCallExpression(node) && node.expression.getText(hookAst) === 'webClient.on' && node.arguments[0]?.text === name,
    hookAst,
    `${name} callback`,
  ).arguments[1];
}
const statusHandler = eventCallback('chat.processing_status').getText(hookAst);
const finalCleanup = findNode(
  node =>
    ts.isIfStatement(node) &&
    node.expression.getText(hookAst).includes('executionError') &&
    node.thenStatement.getText(hookAst).includes('drainTaskQueueIfIdle'),
  eventCallback('chat.final'),
  'chat.final error-aware cleanup',
).getText(hookAst);
const finalCleanupJs = ts.transpileModule(finalCleanup, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
const guardText = await readFile(new URL('../src/hooks/requestEventFilter.ts', import.meta.url), 'utf8');
const guardJs = ts.transpileModule(guardText, { compilerOptions: { module: ts.ModuleKind.CommonJS } }).outputText;
const guardContext = vm.createContext({ exports: {} });
vm.runInContext(guardJs, guardContext);

function hookHarness({ activeRequestId = 'r1', processing = true, enterprise = true, transport = 'http', executionError = 'retry exhausted' } = {}) {
  const calls = [];
  const runtime = { isProcessing: processing, executionError, taskQueue: [{ id: 'q1', content: 'next' }] };
  const store = {
    getRuntime: () => runtime,
    setProcessing: (_sid, value) => {
      runtime.isProcessing = value;
      calls.push('processing');
    },
    setExecutionError: (_sid, value) => {
      runtime.executionError = value;
    },
  };
  for (const name of [
    'setThinking',
    'clearSubtasks',
    'stopStreaming',
    'settlePendingToolExecutions',
    'settleHistoricalToolExecutions',
    'removeFromTaskQueue',
  ]) {
    store[name] = () => calls.push(name);
  }
  const context = vm.createContext({
    isEnterprise: () => enterprise,
    getWebTransport: () => transport,
    useChatStore: { getState: () => store },
    useSessionStore: { getState: () => ({ getRuntime: () => ({ mode: 'agent' }) }) },
    useWorkspaceStore: { getState: () => ({ patchSession: () => calls.push('workspace') }) },
    useGoalStore: { getState: () => ({ runtimes: {} }) },
    resolveEventSessionId: payload => payload.session_id,
    shouldHandleCurrentRequestEvent: event => guardContext.exports.shouldHandleRequestEvent(event, { activeRequestId }),
    shouldDropDuplicatedEvent: () => false,
    tryAdoptSupplementStreamRequestId: () => {},
    isCompletedResumeResult: () => false,
    localSendPendingRef: { current: new Set() },
    flushPendingStreamDelta: () => calls.push('flush'),
    updateSession: () => calls.push('session'),
    sendMessageRef: { current: () => calls.push('send') },
    drainTaskQueueIfIdle: () => calls.push('drain'),
    sessionId: 's1',
    currentMode: 'agent',
  });
  const handlerJs = ts.transpileModule(`const handler = ${statusHandler};`, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
  vm.runInContext(`${handlerJs}\nglobalThis.handler = handler;`, context);
  return { calls, runtime, context, handle: context.handler };
}

const fallback = {
  type: 'event',
  event: 'chat.processing_status',
  request_id: 'r1',
  payload: { session_id: 's1', is_processing: false, source: 'http_error_eof' },
};

test('current errored stream settles once after read failure, including error then final', async () => {
  for (const chunks of [[error], [error, final]]) {
    const h = hookHarness();
    const { events, warnings } = await pump(chunks, {
      failRead: true,
      onEvent(event) {
        if (event.event === 'chat.processing_status') h.handle(event);
      },
    });
    const statuses = events.filter(e => e.event === 'chat.processing_status');
    assert.equal(statuses.length, 1);
    assert.equal(statuses[0].request_id, 'r1');
    assert.equal(statuses[0].payload.source, 'http_error_eof');
    assert.equal(h.runtime.isProcessing, false);
    assert.equal(h.runtime.executionError, 'retry exhausted');
    assert.equal(h.calls.includes('send'), false);
    assert.equal(warnings.length, 1);
  }
});

test('paused enterprise error and terminal state replay in order after resume', async () => {
  for (const ending of ['terminal', 'eof', 'read-error']) {
    const h = hookHarness({ processing: false, executionError: null });
    const { events, buffered, client } = await pump([error, final, ...(ending === 'terminal' ? [end] : [])], {
      paused: true,
      failRead: ending === 'read-error',
      onEvent(event) {
        if (event.event === 'chat.error') h.runtime.executionError = event.payload.error;
        if (event.event === 'chat.final') vm.runInContext(finalCleanupJs, h.context);
        if (event.event === 'chat.processing_status') h.handle(event);
      },
    });
    assert.deepEqual(events, []);
    assert.deepEqual(
      buffered.map(e => e.event),
      ['chat.error', 'chat.final', 'chat.processing_status'],
    );
    assert.equal(h.runtime.isProcessing, false);
    // Existing resume(has_active_task=true) restores processing before replaying the buffer.
    h.runtime.isProcessing = true;
    for (const event of buffered) client.replayBufferedEvent(event);
    assert.equal(h.runtime.isProcessing, false);
    assert.equal(h.runtime.executionError, 'retry exhausted');
    assert.equal(h.calls.filter(name => name === 'send').length, ending === 'terminal' ? 1 : 0);
  }
});

test('EOF hook settles display state once, preserves error, and never sends queued work', () => {
  const h = hookHarness();
  h.handle(fallback);
  assert.equal(h.runtime.isProcessing, false);
  assert.equal(h.runtime.executionError, 'retry exhausted');
  assert.deepEqual(h.calls, [
    'flush',
    'processing',
    'session',
    'workspace',
    'setThinking',
    'clearSubtasks',
    'stopStreaming',
    'settlePendingToolExecutions',
    'settleHistoricalToolExecutions',
  ]);
  const previous = [...h.calls];
  h.handle(fallback);
  assert.deepEqual(h.calls, previous);
});

test('old request EOF cannot settle the new active task', () => {
  const h = hookHarness({ activeRequestId: 'r2' });
  h.handle(fallback);
  assert.equal(h.runtime.isProcessing, true);
  assert.deepEqual(h.calls, []);
});

test('real completion retains existing queue dispatch', () => {
  const h = hookHarness();
  h.handle({ ...fallback, payload: { session_id: 's1', is_processing: false } });
  assert.equal(h.runtime.isProcessing, false);
  assert.equal(h.calls.filter(name => name === 'send').length, 1);
});

test('enterprise final after error preserves failure and waits for task end; other finals retain behavior', () => {
  for (const options of [{}, { executionError: null }, { enterprise: false }, { transport: 'websocket' }]) {
    const h = hookHarness(options);
    const js = ts.transpileModule(finalCleanup, { compilerOptions: { target: ts.ScriptTarget.ES2020 } }).outputText;
    vm.runInContext(js, h.context);
    const failedEnterprise = options.executionError !== null && options.enterprise !== false && options.transport !== 'websocket';
    assert.equal(h.runtime.isProcessing, failedEnterprise);
    assert.equal(h.runtime.executionError, failedEnterprise ? 'retry exhausted' : null);
    assert.equal(h.calls.includes('drain'), !failedEnterprise);
  }
});
