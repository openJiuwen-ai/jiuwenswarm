import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { afterEach, beforeEach, test } from 'node:test';
import { build } from 'esbuild';
import { act, createElement, createRef } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

const panelRoot = resolve('../../../extensions/video_duplex/frontend/VideoLivePanel');
const outfile = resolve('node_modules/.cache/voice-task-panel/panel.mjs');
await build({
  stdin: {
    contents: `export { VideoLivePanel } from ${JSON.stringify(`${panelRoot}/index.tsx`)};
      export { RealtimeDuplexSession } from ${JSON.stringify(`${panelRoot}/qwenOmniSession.ts`)};`,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  outfile,
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  jsx: 'automatic',
  loader: { '.css': 'empty' },
  plugins: [
    {
      name: 'rpc-boundary',
      setup(builder) {
        builder.onResolve({ filter: /services\/webClient$/ }, () => ({ path: 'rpc', namespace: 'test' }));
        builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({
          contents: `export const webClient = { on: (...args) => globalThis.__panel.on(...args) };
          export const webRequest = (...args) => globalThis.__panel.request(...args);`,
          loader: 'js',
        }));
      },
    },
  ],
});
const { VideoLivePanel, RealtimeDuplexSession } = await import(pathToFileURL(outfile));
let dom, root, panel, session, requests, sent, pending, listeners;
const globals = new Map();
const originalStart = RealtimeDuplexSession.prototype.start;

beforeEach(async () => {
  dom = new JSDOM('<div id="root"></div>', { url: 'http://127.0.0.1:5384/' });
  for (const [key, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    navigator: { mediaDevices: { getUserMedia() {} } },
    WebSocket: { OPEN: 1 },
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    globals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  window.AudioWorkletNode = class {};
  window.HTMLElement.prototype.scrollIntoView = () => {};
  window.HTMLMediaElement.prototype.pause = () => {};
  window.HTMLMediaElement.prototype.load = () => {};
  window.setInterval = () => 1;
  window.clearInterval = () => {};
  requests = [];
  sent = [];
  pending = new Map();
  listeners = new Map();
  globalThis.__panel = {
    on(name, callback) {
      listeners.set(name, callback);
      return () => listeners.delete(name);
    },
    async request(method, args) {
      requests.push({ method, args });
      if (method === 'video.realtime.config') return { provider: 'qwen_omni', url: 'ws://127.0.0.1/realtime' };
      if (method === 'video.qwen.tool') return new Promise((resolve) => pending.set(args.call_id, resolve));
      return {};
    },
  };
  // Replace only device/network startup. Parsing, response scheduling and all
  // actual component callbacks remain real.
  RealtimeDuplexSession.prototype.start = async function () {
    session = this;
    this.socket = { readyState: 1, send: (value) => sent.push(JSON.parse(value)), close() {} };
    this.sessionReady = true;
    this.callbacks.onState('listening');
  };
  panel = createRef();
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(createElement(VideoLivePanel, { ref: panel })));
  await start();
});

afterEach(async () => {
  await act(async () => root.unmount());
  RealtimeDuplexSession.prototype.start = originalStart;
  delete globalThis.__panel;
  dom.window.close();
  for (const [key, descriptor] of globals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
  globals.clear();
});

async function start() {
  await act(async () => document.querySelector('[aria-label="开启 Full-duplex 会话"]').click());
  assert.ok(session);
}

async function call(name, args, id) {
  await act(async () =>
    session.handleEvent({
      type: 'response.function_call_arguments.done',
      name,
      arguments: JSON.stringify(args),
      call_id: id,
    }),
  );
  return requests.findLast((request) => request.method === 'video.qwen.tool').args;
}

async function receipt(id, payload) {
  await act(async () => pending.get(id)(payload));
}

async function finishResponse() {
  await act(async () => {
    session.handleEvent({ type: 'response.created', response: { id: 'ack' } });
    session.handleEvent({ type: 'response.done', response: { id: 'ack' } });
  });
}

const outputs = () => sent.filter((event) => event.item?.type === 'function_call_output');

test('page forwards captured input, acknowledges acceptance once, and delivers completion once', async () => {
  await act(async () => {
    session.handleEvent({ type: 'input_audio_buffer.speech_started', item_id: 'input-a' });
    session.handleEvent({ type: 'input_audio_buffer.speech_stopped' });
    session.handleEvent({
      type: 'conversation.item.input_audio_transcription.completed',
      item_id: 'input-a',
      transcript: '计算一到十',
    });
    session.handleEvent({ type: 'response.created', response: { id: 'response-a' } });
  });
  const args = await call('jiuwen_delegate', { task: '计算1到10的和' }, 'create');
  assert.equal(args.question, '计算一到十');
  assert.equal(args.turn_id, 'input-a');
  assert.equal(outputs().length, 0, 'RPC has not accepted the task yet');
  await receipt('create', { search_job: { id: 'task-a', status: 'queued', question: '计算一到十' } });
  await finishResponse();
  assert.equal(outputs().length, 1);
  assert.equal(JSON.parse(outputs()[0].item.output).state, 'accepted');
  await finishResponse();
  const result = {
    job_id: 'task-a',
    search_session_id: args.search_session_id,
    question: '计算一到十',
    status: 'completed',
    result: '55',
    tool_call_id: 'create',
    realtime_brief: { status: 'completed', summary: '55' },
  };
  await act(async () => {
    panel.current.deliverToolResult(result);
    panel.current.deliverToolResult(result);
  });
  assert.equal(outputs().length, 1);
  assert.equal(
    sent.filter((event) => event.item?.content?.[0]?.text?.includes('Jiuwen result delivery notice')).length,
    1,
  );
});

test('modify receipt registers successor for completion without inventing another tool call', async () => {
  const args = await call('jiuwen_task_modify', { job_id: 'parent', revision: 1, instruction: '改为66' }, 'modify');
  await receipt('modify', { tool_result: { state: 'followup', successor_id: 'child' } });
  await finishResponse();
  const result = {
    job_id: 'child',
    search_session_id: args.search_session_id,
    status: 'completed',
    question: '改为66',
    result: '66',
    realtime_brief: { status: 'completed', summary: '66' },
  };
  await act(async () => panel.current.deliverToolResult(result));
  assert.equal(outputs().length, 1);
  assert.equal(outputs()[0].item.call_id, 'modify');
  assert.ok(sent.some((event) => event.item?.content?.[0]?.text?.includes('66')));
});

test('observed question and spoken answer retain exact task and interaction identities through RPC', async () => {
  const args = await call('jiuwen_task_query', {}, 'query');
  await receipt('query', { tool_result: { jobs: [] } });
  await finishResponse();
  const interaction = {
    id: 'interaction-a',
    request_id: 'core-question',
    state: 'pending',
    questions: [{ question: '预算？' }],
  };
  await act(async () =>
    listeners.get('video.search.progress')({
      payload: {
        job_id: 'trip',
        search_session_id: args.search_session_id,
        status: 'waiting_user',
        interaction,
      },
    }),
  );
  assert.ok(sent.some((event) => event.item?.content?.[0]?.text?.includes('interaction-a')));
  const answer = { job_id: 'trip', interaction_id: 'interaction-a', answers: ['5000元'] };
  const forwarded = await call('jiuwen_task_answer', answer, 'answer');
  assert.deepEqual(JSON.parse(forwarded.arguments), answer);
  await receipt('answer', { tool_result: { state: 'accepted', task_id: 'trip' } });
  await finishResponse();
  assert.equal(
    requests.filter((request) => request.method === 'video.qwen.tool' && request.args.name === 'jiuwen_delegate')
      .length,
    0,
  );
  assert.equal(JSON.parse(outputs().at(-1).item.output).state, 'accepted');
});

test('late RPC receipt after media restart cannot speak into the new session', async () => {
  await call('jiuwen_delegate', { task: 'old task' }, 'old');
  const previous = session;
  await act(async () => panel.current.stop());
  await start();
  assert.notEqual(session, previous);
  await receipt('old', { search_job: { id: 'old-task', status: 'queued', question: 'old task' } });
  assert.equal(outputs().length, 0);
});
