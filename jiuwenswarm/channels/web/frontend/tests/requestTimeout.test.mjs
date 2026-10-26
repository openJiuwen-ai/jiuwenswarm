import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { build } from 'esbuild';

globalThis.window = {
  __JIUWENSWARM_EDITION__: 'personal',
  __JIUWEN_WEB_TRANSPORT__: 'websocket',
  location: new URL('http://localhost/'),
};

// Exercise the real transports and timeout reader; only translation is replaced.
const bundle = await build({
  absWorkingDir: fileURLToPath(new URL('../', import.meta.url)),
  stdin: {
    contents: `export { WebHttpClient } from './src/services/webHttpClient';
      export { webClient } from './src/services/webClient';
      export { getDefaultRequestTimeoutMs } from './src/utils/requestTimeout';`,
    resolveDir: fileURLToPath(new URL('../', import.meta.url)),
  },
  bundle: true,
  write: false,
  platform: 'node',
  format: 'esm',
  define: { 'import.meta.env': '{}' },
  plugins: [
    {
      name: 'translation-only',
      setup(builder) {
        builder.onResolve({ filter: /^\.\.\/i18n$/ }, () => ({ path: 'i18n', namespace: 'translation' }));
        builder.onLoad({ filter: /.*/, namespace: 'translation' }, () => ({ contents: 'export default { t: key => key };' }));
      },
    },
  ],
});
const { WebHttpClient, webClient, getDefaultRequestTimeoutMs } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);
const WebClient = webClient.constructor;

function browserTimers(value) {
  window.__JIUWEN_WEB_REQUEST_TIMEOUT_SECONDS__ = value;
  const timers = new Map();
  let nextId = 0;
  window.setTimeout = (callback, delay) => {
    const id = ++nextId;
    timers.set(id, { callback, delay });
    return id;
  };
  window.clearTimeout = id => timers.delete(id);
  return timers;
}

test('missing, malformed and overflowing deploy values retain the 15-second default', t => {
  t.mock.method(console, 'warn', () => {});
  for (const value of [
    undefined,
    '',
    '  ',
    '__JIUWEN_WEB_REQUEST_TIMEOUT_SECONDS_VALUE__',
    '0',
    '-1',
    '1.5',
    'NaN',
    'Infinity',
    '2147484',
    '9'.repeat(100),
    "';alert(1)//",
  ]) {
    browserTimers(value);
    assert.equal(getDefaultRequestTimeoutMs(), 15000, String(value));
  }
});

test('runtime seconds are converted to milliseconds without rebuilding', () => {
  for (const [value, expected] of [
    ['60', 60000],
    [' 30 ', 30000],
    ['1', 1000],
    ['2147483', 2147483000],
  ]) {
    browserTimers(value);
    assert.equal(getDefaultRequestTimeoutMs(), expected);
  }
});

for (const [configured, override, expected] of [
  [undefined, undefined, 15000],
  ['60', undefined, 60000],
  ['60', 120000, 120000],
]) {
  test(`HTTP cancels stalled headers at ${expected}ms and preserves explicit overrides`, async t => {
    const timers = browserTimers(configured);
    const client = new WebHttpClient();
    client.state = 'ready';
    let requestSignal;
    t.mock.method(
      globalThis,
      'fetch',
      (_url, init) =>
        new Promise((_resolve, reject) => {
          requestSignal = init.signal;
          init.signal.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')), { once: true });
        }),
    );
    const pending = client.request('skills.list', {}, override === undefined ? {} : { timeoutMs: override });
    const rejected = assert.rejects(pending, error => error.code === 'REQUEST_TIMEOUT');
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(timers.size, 1);
    assert.equal([...timers.values()][0].delay, expected);
    [...timers.values()][0].callback();
    await rejected;
    assert.equal(requestSignal.aborted, true);
    assert.equal(client.getInflightCount(), 0);
    assert.equal(timers.size, 0);
  });
}

test('receiving SSE headers clears the deadline while the body can continue later', async t => {
  const timers = browserTimers('1');
  const client = new WebHttpClient();
  client.state = 'ready';
  let body;
  let signal;
  const stream = new ReadableStream({
    start(controller) {
      body = controller;
    },
  });
  t.mock.method(globalThis, 'fetch', async (_url, init) => {
    signal = init.signal;
    return new Response(stream, { headers: { 'Content-Type': 'text/event-stream' } });
  });
  const received = new Promise(resolve => client.on('chat.delta', resolve));
  assert.equal((await client.request('chat.send', { session_id: 's1', content: 'hello' })).accepted, true);
  assert.equal(timers.size, 0, 'no request timeout remains after response headers');
  body.enqueue(new TextEncoder().encode('event: chat.delta\ndata: {"session_id":"s1","delta":"hello"}\n\n'));
  await received;
  assert.equal(signal.aborted, false);
  body.close();
  await new Promise(resolve => setImmediate(resolve));
  await client.disconnect();
});

test('WebSocket requests use the same configured default', async () => {
  const timers = browserTimers('60');
  const client = new WebClient();
  client.state = 'ready';
  client.ws = { readyState: WebSocket.OPEN, send() {} };
  const pending = client.request('skills.list');
  const rejected = assert.rejects(pending, error => error.code === 'REQUEST_TIMEOUT');
  await new Promise(resolve => setImmediate(resolve));
  assert.equal([...timers.values()][0].delay, 60000);
  [...timers.values()][0].callback();
  await rejected;
});
