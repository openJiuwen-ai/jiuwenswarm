import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { after, test } from 'node:test';
import { setImmediate as settle } from 'node:timers/promises';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('', { url: 'http://localhost/chat/new?user_id=user1&group_id=group1&bot_id=bot1' });
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = dom.window.localStorage;
window.__JIUWENSWARM_EDITION__ = 'enterprise';
window.__JIUWEN_WEB_TRANSPORT__ = 'http';
after(() => dom.window.close());

await build({
  entryPoints: ['src/features/cron/cronSessionLive.ts', 'src/services/webClient.ts', 'src/stores/chatStore.ts'],
  bundle: true, splitting: true, packages: 'external', platform: 'node', format: 'esm',
  outdir: 'node_modules/.cache/cron-session-live', outExtension: { '.js': '.mjs' },
  define: { 'import.meta.env': '{}' },
});
const { watchCronExecutionSession } = await import('../node_modules/.cache/cron-session-live/features/cron/cronSessionLive.mjs');
const { webClient } = await import('../node_modules/.cache/cron-session-live/services/webClient.mjs');
const { useChatStore } = await import('../node_modules/.cache/cron-session-live/stores/chatStore.mjs');

const history = {
  nextCursor: null, hasMore: false, snapshotId: 'history-snapshot', snapshotEnd: 10,
  loadedBatchSeq: 1, publishedBatchSeq: 1, historyComplete: true,
};
const completion = [
  ['chat.delta', { content: 'new output', live_seq: 11 }],
  ['chat.final', { content: 'new complete output', live_seq: 12 }],
  ['chat.processing_status', { is_processing: false, live_seq: 13 }],
];

function frame(event, payload) {
  return new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(payload)}\n\n`);
}

async function fixture(run) {
  const previousFetch = globalThis.fetch;
  const previousState = useChatStore.getState();
  const received = [];
  const streams = [];
  const requests = [];
  const off = ['chat.delta', 'chat.final', 'chat.processing_status', 'chat.ask_user_question']
    .map(name => webClient.on(name, event => received.push(event)));
  let stop = () => {};
  globalThis.fetch = async (url, init) => {
    let controller;
    const body = new ReadableStream({ start(value) { controller = value; } });
    const stream = {
      closed: false,
      send(event, payload) { controller.enqueue(frame(event, payload)); },
      close() { if (!this.closed) { this.closed = true; controller.close(); } },
    };
    streams.push(stream);
    requests.push({ url, init });
    return new Response(body, { headers: { 'content-type': 'text/event-stream' } });
  };
  const sid = '__cron__history-live-test';
  useChatStore.getState().ensureRuntime(sid);
  const control = {
    sid, received, streams, requests,
    async start() { stop = watchCronExecutionSession(sid); await settle(); return streams.at(-1); },
    showHistory() { useChatStore.getState().setHistoryPagerMeta(sid, history); },
  };
  try {
    await run(control);
  } finally {
    stop();
    streams.forEach(stream => stream.close());
    off.forEach(dispose => dispose());
    globalThis.fetch = previousFetch;
    useChatStore.setState(previousState, true);
    await settle();
  }
}

for (const withHistory of [false, true]) {
  test(`the real listener forwards delta, final and terminal status with history=${withHistory}`, () => fixture(async control => {
    if (withHistory) control.showHistory();
    const stream = await control.start();
    completion.forEach(([name, payload]) => stream.send(name, payload));
    await settle();
    assert.deepEqual(control.received.map(event => event.event), completion.map(([name]) => name));
    assert.equal(control.received.at(-1).payload.is_processing, false);
    assert.ok(control.received.every(event => event.payload.session_id === control.sid));
  }));
}

test('history becoming available during an open SSE stream does not drop subsequent output', () => fixture(async control => {
  const stream = await control.start();
  stream.send('chat.delta', { content: 'before history', live_seq: 1 });
  await settle();
  control.showHistory();
  completion.forEach(([name, payload]) => stream.send(name, payload));
  await settle();
  assert.deepEqual(control.received.map(event => event.event), ['chat.delta', ...completion.map(([name]) => name)]);
  assert.equal(control.received[1].payload.content, 'new output');
}));

test('placeholder output is still ignored while a live question is retained after history', () => fixture(async control => {
  control.showHistory();
  const stream = await control.start();
  stream.send('chat.final', { content: 'placeholder', cron: { is_placeholder: true } });
  stream.send('chat.ask_user_question', { questions: [{ question: 'continue?' }] });
  stream.send('unrelated.event', { content: 'not a cron chat event' });
  await settle();
  assert.deepEqual(control.received.map(event => event.event), ['chat.ask_user_question']);
}));

test('initial buffered output does not duplicate an already restored answer or tool history', () => fixture(async control => {
  control.showHistory();
  useChatStore.getState().addMessage(control.sid, {
    id: 'stored-answer', role: 'assistant', content: 'stored answer', timestamp: new Date().toISOString(),
  });
  const stream = await control.start();
  for (const [index, event] of ['chat.delta', 'chat.tool_call', 'chat.final'].entries()) {
    stream.send(event, { content: 'stored answer', live_seq: index + 1, live_replay: true });
  }
  stream.send('chat.processing_status', { is_processing: false, live_seq: 4, live_replay: true });
  stream.send('chat.ask_user_question', { questions: [{ question: 'continue?' }], live_seq: 5, live_replay: true });
  await settle();
  assert.deepEqual(control.received.map(event => event.event), ['chat.processing_status', 'chat.ask_user_question']);
  assert.deepEqual(useChatStore.getState().getRuntime(control.sid).messages.map(message => message.content), ['stored answer']);
}));

test('a buffered final missing from an incomplete history is retained', () => fixture(async control => {
  control.showHistory();
  const stream = await control.start();
  stream.send('chat.final', { content: 'answer persisted after the snapshot', live_seq: 1, live_replay: true });
  await settle();
  assert.equal(control.received.length, 1);
  assert.equal(control.received[0].payload.content, 'answer persisted after the snapshot');
}));

test('live sequence dedupe keeps identical tokens with distinct sequences', () => fixture(async control => {
  const stream = await control.start();
  stream.send('chat.delta', { content: '哈', live_seq: 1 });
  stream.send('chat.delta', { content: '哈', live_seq: 2 });
  stream.send('chat.delta', { content: '哈', live_seq: 2, live_replay: true });
  await settle();
  assert.deepEqual(control.received.map(event => event.payload.content), ['哈', '哈']);
}));

test('reconnecting a history-backed session continues to forward live frames', () => fixture(async control => {
  control.showHistory();
  const stream = await control.start();
  stream.send('chat.delta', { content: 'first connection', live_seq: 1 });
  stream.close();
  for (let attempt = 0; attempt < 30 && control.streams.length < 2; attempt++) {
    await new Promise(resolve => setTimeout(resolve, 100));
  }
  assert.equal(control.streams.length, 2);
  control.streams[1].send('chat.delta', { content: 'first connection', live_seq: 1, live_replay: true });
  completion.forEach(([name, payload]) => control.streams[1].send(name, { ...payload, live_replay: true }));
  await settle();
  assert.equal(control.received.length, 4);
  assert.equal(control.received.at(-1).payload.is_processing, false);
}));

test('personal WebSocket and non-cron HTTP sessions do not start this listener', () => fixture(async control => {
  window.__JIUWENSWARM_EDITION__ = 'personal';
  window.__JIUWEN_WEB_TRANSPORT__ = 'ws';
  try {
    watchCronExecutionSession(control.sid)();
    window.__JIUWEN_WEB_TRANSPORT__ = 'http';
    watchCronExecutionSession('ordinary-session')();
    await settle();
    assert.equal(control.requests.length, 0);
  } finally {
    window.__JIUWENSWARM_EDITION__ = 'enterprise';
    window.__JIUWEN_WEB_TRANSPORT__ = 'http';
  }
}));

test('history-backed live output is forwarded over an actual HTTP SSE response', async () => {
  const originalFetch = globalThis.fetch;
  const previousState = useChatStore.getState();
  const sid = 'cron_http_history_test';
  const events = [];
  let requestPath;
  let stop = () => {};
  let complete;
  const done = new Promise(resolve => { complete = resolve; });
  const off = completion.map(([name]) => webClient.on(name, event => {
    events.push(event);
    if (events.length === 3) complete();
  }));
  const server = createServer((request, response) => {
    requestPath = request.url;
    response.writeHead(200, { 'content-type': 'text/event-stream', 'cache-control': 'no-cache' });
    completion.forEach(([name, payload]) => response.write(frame(name, payload)));
  });
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  const base = `http://127.0.0.1:${server.address().port}`;
  globalThis.fetch = (input, init) => originalFetch(new URL(input, base), init);
  let timeout;
  try {
    useChatStore.getState().ensureRuntime(sid);
    useChatStore.getState().setHistoryPagerMeta(sid, history);
    stop = watchCronExecutionSession(sid);
    await Promise.race([done, new Promise((_, reject) => {
      timeout = setTimeout(() => reject(new Error('SSE listener did not forward completion')), 3000);
    })]);
    assert.equal(requestPath, `/gateway-api/v1/sessions/${sid}/live`);
    assert.deepEqual(events.map(event => event.event), completion.map(([name]) => name));
    assert.equal(events.at(-1).payload.is_processing, false);
  } finally {
    clearTimeout(timeout);
    stop();
    off.forEach(dispose => dispose());
    globalThis.fetch = originalFetch;
    useChatStore.setState(previousState, true);
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  }
});
