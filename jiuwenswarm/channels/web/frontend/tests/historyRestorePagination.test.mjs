import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { build } from 'esbuild';

const frontendRoot = fileURLToPath(new URL('..', import.meta.url));
const outfile = join(frontendRoot, 'node_modules/.cache/history-restore-pagination/restore.mjs');
await build({
  stdin: {
    contents: 'export { beginHistoryRestore } from "./src/features/historyRestore.ts"; export { webClient } from "./src/services/webClient.ts";',
    resolveDir: frontendRoot,
    loader: 'ts',
  },
  outfile,
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  define: { 'import.meta.env': '{}' },
});

globalThis.window = globalThis;
globalThis.window.location = { protocol: 'http:', host: 'localhost:18180' };

class Socket {
  static OPEN = 1;
  static instance;
  readyState = 0;
  listeners = new Map();

  constructor() {
    Socket.instance = this;
    queueMicrotask(() => {
      this.readyState = Socket.OPEN;
      this.onopen?.();
    });
  }

  send(raw) {
    const request = JSON.parse(raw);
    queueMicrotask(() => this.receive({ type: 'res', id: request.id, ok: true, payload: { accepted: true } }));
  }

  receive(message) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }

  addEventListener(name, callback) {
    const listeners = this.listeners.get(name) ?? [];
    listeners.push(callback);
    this.listeners.set(name, listeners);
  }

  close() {
    this.readyState = 3;
    const event = { code: 1000, reason: '', wasClean: true };
    this.onclose?.(event);
    for (const listener of this.listeners.get('close') ?? []) listener(event);
  }
}

globalThis.WebSocket = Socket;
const { beginHistoryRestore, webClient } = await import(pathToFileURL(outfile).href);
await webClient.connect();
after(() => webClient.disconnect());

let sessionSequence = 0;
const flush = () => new Promise((resolve) => setImmediate(resolve));
const hiddenRecords = Array.from({ length: 50 }, (_, index) => ({
  id: `member-tool-${index}`,
  role: 'teammate',
  mode: 'team',
  member_name: 'researcher',
  event_type: 'chat.tool_call',
  content: '',
  timestamp: 1792980000 + index,
  tool_call: { id: `call-${index}`, name: 'search', arguments: {} },
}));
const visibleRecords = [
  { id: 'user', role: 'user', content: 'Earlier user message', timestamp: 1792979998 },
  { id: 'reply', role: 'assistant', event_type: 'chat.final', content: 'Earlier reply', timestamp: 1792979999 },
];

function restore(overrides = {}) {
  const sessionId = `pagination-${++sessionSequence}`;
  const calls = { ready: [], empty: [], failed: [], requests: [] };
  const handle = beginHistoryRestore({
    sessionId,
    requestNextBatch: (cursor) => {
      calls.requests.push(cursor);
      return webClient.request('history.get', { session_id: sessionId, cursor, limit: 50 });
    },
    onReady: (messages, cursor) => calls.ready.push({ messages, cursor }),
    onEmpty: (cursor) => calls.empty.push(cursor),
    onFailure: (failure) => calls.failed.push(failure),
    ...overrides,
  });
  return { sessionId, calls, handle };
}

function emit(sessionId, event, payload) {
  Socket.instance.receive({ type: 'event', event, payload: { session_id: sessionId, ...payload } });
}

function batch(state, cursor, records, nextCursor, extra = {}) {
  const meta = {
    cursor,
    subagent_id: '',
    next_cursor: nextCursor,
    has_more: nextCursor !== null,
    snapshot_id: 'snapshot',
    snapshot_end: 100000,
    ...extra,
  };
  for (const message of records) emit(state.sessionId, 'history.message', { ...meta, message });
  emit(state.sessionId, 'history.message', { ...meta, status: 'done' });
}

test('initial restore skips filtered batches and publishes older messages before buffered live events', async () => {
  const state = restore();
  const live = [];
  const unsubscribe = webClient.on('chat.final', (event) => live.push(event.payload.content));
  try {
    emit(state.sessionId, 'chat.final', { content: 'Live reply' });
    let cursor = null;
    for (const nextCursor of ['older-1', 'older-2', 'older-3']) {
      batch(state, cursor, hiddenRecords, nextCursor);
      await flush();
      assert.equal(state.calls.ready.length, 0);
      assert.equal(state.calls.empty.length, 0);
      assert.equal(live.length, 0);
      cursor = nextCursor;
    }
    batch(state, null, [], null);
    assert.equal(state.calls.empty.length, 0);
    batch(state, cursor, visibleRecords, null);
    assert.deepEqual(state.calls.requests, ['older-1', 'older-2', 'older-3']);
    assert.deepEqual(state.calls.ready[0].messages.map((message) => message.content), ['Earlier user message', 'Earlier reply']);
    assert.equal(state.calls.ready.length, 1);
    assert.equal(state.calls.ready[0].cursor.requestCursor, 'older-3');
    assert.equal(state.calls.ready[0].cursor.hasMore, false);
    assert.equal(state.calls.empty.length, 0);
    assert.equal(state.calls.failed.length, 0);
    await flush();
    assert.deepEqual(live, ['Live reply']);
  } finally {
    state.handle.dispose();
    unsubscribe();
  }
});

test('a non-empty first batch preserves its next cursor for ordinary pagination', async () => {
  const state = restore();
  batch(state, null, visibleRecords, 'older-1');
  await flush();
  assert.equal(state.calls.ready.length, 1);
  assert.equal(state.calls.ready[0].cursor.nextCursor, 'older-1');
  assert.deepEqual(state.calls.requests, []);
  assert.deepEqual(state.calls.empty, []);
  state.handle.dispose();
});

test('empty is reported once only after every filtered batch is exhausted', async () => {
  const state = restore();
  batch(state, null, [], 'older-1');
  await flush();
  assert.deepEqual(state.calls.empty, []);
  batch(state, 'older-1', hiddenRecords, 'older-2');
  await flush();
  assert.deepEqual(state.calls.empty, []);
  batch(state, 'older-2', hiddenRecords, null);
  assert.equal(state.calls.empty.length, 1);
  assert.equal(state.calls.empty[0].hasMore, false);
  assert.deepEqual(state.calls.ready, []);
  state.handle.dispose();
});

test('a failed continuation reports failure instead of declaring the session empty', async () => {
  const state = restore({ requestNextBatch: async () => { throw new Error('Batch request failed'); } });
  batch(state, null, hiddenRecords, 'older-1');
  await flush();
  assert.deepEqual(state.calls.empty, []);
  assert.deepEqual(state.calls.ready, []);
  assert.deepEqual(state.calls.failed, [{ code: 'HISTORY_RESTORE_FAILED', message: 'Batch request failed' }]);
  state.handle.dispose();
});

test('continuation cannot combine different history snapshots', async () => {
  const state = restore();
  batch(state, null, hiddenRecords, 'older-1');
  await flush();
  batch(state, 'older-1', visibleRecords, null, { snapshot_id: 'different-snapshot' });
  assert.equal(state.calls.failed[0].code, 'HISTORY_SNAPSHOT_CHANGED');
  assert.deepEqual(state.calls.ready, []);
  assert.deepEqual(state.calls.empty, []);
  state.handle.dispose();
});

test('disposing an unfinished initial restore cancels its continuation', async () => {
  const state = restore();
  batch(state, null, hiddenRecords, 'older-1');
  state.handle.dispose();
  await flush();
  batch(state, 'older-1', visibleRecords, null);
  assert.deepEqual(state.calls, { ready: [], empty: [], failed: [], requests: [] });
});

test('each continuation retains the original per-batch restore timeout', async (context) => {
  context.mock.timers.enable({ apis: ['setTimeout'] });
  const state = restore();
  try {
    context.mock.timers.tick(29999);
    batch(state, null, hiddenRecords, 'older-1');
    await flush();
    context.mock.timers.tick(29999);
    assert.deepEqual(state.calls.failed, []);
    batch(state, 'older-1', visibleRecords, null);
    assert.equal(state.calls.ready.length, 1);
  } finally {
    state.handle.dispose();
    context.mock.timers.reset();
  }
});
