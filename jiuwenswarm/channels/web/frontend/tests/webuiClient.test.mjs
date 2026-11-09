import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { createServer } from 'node:http';
import { once } from 'node:events';
import { createClientCapabilityHost, createGatewayFetch, isPluginResourceURL, resolvePluginResourceURL, browserClientCapabilities, webClient, gitWatchClient, systemPluginApi } from '../node_modules/.cache/webui-client/client.mjs';

class Storage {
  data = new Map(); reads = 0;
  getItem(key) { this.reads++; return this.data.get(key) ?? null; }
  setItem(key, value) { this.data.set(key, String(value)); }
  removeItem(key) { this.data.delete(key); }
}
const http = { type: 'http', source: { key: 'record', path: ['credential'] }, target: { location: 'header', name: 'Authorization', prefix: 'Bearer ' } };
const websocket = { type: 'websocket', source: { key: 'record', path: ['credential'] },
  target: { location: 'message', path: ['request_context', 'headers', 'Authorization'], prefix: 'Bearer ' } };
const declarations = (bindings = [http, websocket], id = 'gateway-one') => ({ clientProtocolVersion: 1,
  gatewayInstanceId: id, connectionEpoch: 'connection-1', packages: [
    { packageId: 'pkg.one', activationGeneration: 'generation-1', requestBindings: bindings },
    { packageId: 'pkg.two', activationGeneration: 'generation-2', requestBindings: [] },
  ] });
const operation = (value, extra = {}) => ({ clientProtocolVersion: 1, invocation_id: 'control-request',
  connection_epoch: 'connection-1', package_id: 'pkg.one', activation_generation: 'generation-1',
  method: 'local_storage.set', params: { key: 'record', value }, deadline: Date.now() + 30_000, ...extra });
async function setup(bindings = [http, websocket], options = {}) {
  const storage = options.storage ?? new Storage();
  const host = createClientCapabilityHost({ storage: () => storage, gatewayUrl: () => 'https://gateway.test/', ...options });
  host.configure(declarations(bindings));
  await Promise.resolve();
  return { host, storage, async set(value) { assert.equal(host.acceptOperation(operation(value)), true); await Promise.resolve(); } };
}

test('localStorage isolates packages and Gateway identities and deletes the execution-time value', async () => {
  const app = await setup([]);
  await app.set({ credential: 'one', revision: 3 });
  assert.equal(app.host.acceptOperation(operation({ credential: 'two' }, { package_id: 'pkg.two', activation_generation: 'generation-2' })), true);
  assert.equal(app.storage.data.size, 2);
  await app.set({ credential: 'newer', revision: 1 });
  assert.equal(app.host.acceptOperation(operation(undefined, { method: 'local_storage.delete', params: { key: 'record' } })), true);
  assert.equal(app.storage.data.size, 1);
  app.host.configure(declarations([], 'gateway-two'));
  await app.set({ credential: 'different-gateway' });
  assert.equal(app.storage.data.size, 2);
});

test('client declarations preserve ordinary WebUI packages while retaining activation checks for participating packages', async () => {
  const app = await setup([]);
  const ordinary = { target: { package_id: 'webui.workbench-v2', scope: 'session',
    session_id: 'conversation', slot_id: 'conversation.header.actions' }, revision: 1 };
  assert.equal(app.host.acceptsFrame(ordinary), true);
  assert.equal(app.host.acceptsFrame({ ...ordinary, target: { ...ordinary.target,
    scope: 'application', slot_id: 'application.sidebar.footer' } }), true);
  assert.equal(app.host.acceptsFrame({ ...ordinary, activation_generation: 'unknown' }), false);
  const managed = { target: { package_id: 'pkg.one' }, activation_generation: 'generation-1' };
  assert.equal(app.host.acceptsFrame(managed), true);
  assert.equal(app.host.acceptsFrame({ target: managed.target }), false);
  assert.equal(app.host.acceptsFrame({ ...managed, activation_generation: 'old' }), false);
  const removed = declarations([]); removed.packages = [];
  app.host.configure(removed);
  assert.equal(app.host.acceptsFrame(managed), false);
  assert.equal(app.host.acceptsFrame(ordinary), true);
  assert.throws(() => app.host.configure({}));
  assert.equal(app.host.acceptsFrame(ordinary), true, 'ordinary AgentServer frames do not depend on Gateway client declarations');
  assert.equal(app.host.acceptsFrame(managed), false);
});

test('finite JSON, key/size/depth limits and native storage failures never change the old record', async () => {
  const app = await setup([]);
  await app.set({ old: true });
  const before = [...app.storage.data];
  const circular = {}; circular.self = circular;
  for (const value of [undefined, NaN, Infinity, new Date(), () => {}, circular, { a: undefined }, 'x'.repeat(65537)]) {
    assert.equal(app.host.acceptOperation(operation(value)), false);
    assert.deepEqual([...app.storage.data], before);
  }
  assert.equal(app.host.acceptOperation(operation('x', { params: { key: 'x'.repeat(129), value: 'x' } })), false);
  app.storage.setItem = () => { throw new DOMException('test', 'QuotaExceededError'); };
  assert.equal(app.host.acceptOperation(operation('x')), false);
  assert.equal(app.host.getError(), 'QUOTA_EXCEEDED');
  assert.deepEqual([...app.storage.data], before);
});

test('rejects stale packages/connections, unknown methods, and expired commands', async () => {
  const app = await setup([]);
  for (const change of [{ package_id: 'other' }, { activation_generation: 'old' }, { connection_epoch: 'old' },
    { method: 'local_storage.clear' }, { deadline: Date.now() - 1 }, { params: { key: 'x', value: 1, extra: true } }]) {
    assert.equal(app.host.acceptOperation(operation('x', change)), false);
  }
  assert.equal(app.storage.data.size, 0);
});

test('bindings select only their transport, preserve params and read a shared source once per request', async () => {
  const app = await setup();
  assert.equal(app.host.bindWebSocket({ method: 'config.get' }).request_context, undefined);
  await app.set({ credential: 'example' });
  app.storage.reads = 0;
  const headers = app.host.bindHTTP('https://gateway.test/api', { Accept: 'application/json' });
  assert.equal(headers.get('Authorization'), 'Bearer example');
  assert.equal(app.storage.reads, 1);
  app.storage.reads = 0;
  const params = { value: 'unchanged' };
  const message = app.host.bindWebSocket({ method: 'config.get', params });
  assert.deepEqual(message.request_context, { headers: { Authorization: 'Bearer example' } });
  assert.equal(message.params, params);
  assert.equal(app.storage.reads, 1);
  const onlyHTTP = await setup([http]); await onlyHTTP.set({ credential: 'http' });
  assert.equal(onlyHTTP.host.bindWebSocket({ method: 'config.get' }).request_context, undefined);
  const onlyWS = await setup([websocket]); await onlyWS.set({ credential: 'ws' });
  assert.equal(onlyWS.host.bindHTTP('https://gateway.test/api').has('Authorization'), false);
  app.storage.reads = 0;
  assert.equal(app.host.bindHTTP('https://external.test/api').has('Authorization'), false);
  assert.equal(app.storage.reads, 0);
});

test('published resource addresses are independent of the browser capability package list', async () => {
  const app = await setup([http], { gatewayUrl: () => 'https://gateway.test/api/' });
  await app.set({ credential: 'one' });
  for (const path of ['pkg.one/generation-1/logo.svg', '_agent/route/resource/page.html', 'unknown/old/file.css']) {
    const url = 'https://gateway.test/api/extension-assets/' + path;
    assert.equal(isPluginResourceURL(url, app.host.getGatewayUrl()), true);
    assert.equal(app.host.bindHTTP(url).has('Authorization'), false);
  }
  assert.equal(app.host.bindHTTP('https://gateway.test/api/protected.png').get('Authorization'), 'Bearer one');
  for (const url of ['https://external.test/api/extension-assets/p/g/a.png',
    'https://gateway.test/extension-assets/p/g/a.png', 'https://gateway.test/api/extension-assets-copy/p/g/a.png']) {
    assert.equal(isPluginResourceURL(url, app.host.getGatewayUrl()), false);
  }
  assert.equal(resolvePluginResourceURL('/extension-assets/_agent/r/id/en.html', 'https://gateway.test/api/', 'file:///app/index.html'),
    'https://gateway.test/api/extension-assets/_agent/r/id/en.html');
});

test('published file loads survive credential and capability declaration changes; their caller owns cancellation', async () => {
  for (const path of ['pkg.one/generation-1/a.svg', '_agent/route/resource/en.html']) {
    const app = await setup([http]);
    let finish;
    let request;
    const boundFetch = createGatewayFetch(app.host, (input, init) => {
      request = init;
      return new Promise((resolve) => { finish = resolve; });
    });
    const caller = new AbortController();
    const pending = boundFetch('https://gateway.test/extension-assets/' + path, { signal: caller.signal });
    assert.equal(request.headers.has('Authorization'), false);
    assert.equal(request.redirect, 'error');
    await app.set({ credential: 'changed' });
    app.host.configure({ ...declarations([]), packages: [] });
    app.host.suspendConnection();
    assert.equal(request.signal.aborted, false);
    finish(new Response('file'));
    const response = await pending;
    caller.abort();
    await assert.rejects(response.text(), { name: 'AbortError' });
  }
});

test('missing, unreadable, malformed, wrong-type and invalid header values are omitted', async () => {
  const app = await setup([http]);
  assert.equal(app.host.bindHTTP('https://gateway.test/api').has('Authorization'), false);
  for (const value of [{}, { credential: 12 }, { credential: 'bad\r\nvalue' }, { credential: '中文' }]) {
    await app.set(value);
    assert.equal(app.host.bindHTTP('https://gateway.test/api').has('Authorization'), false);
  }
  const key = [...app.storage.data.keys()][0];
  app.storage.data.set(key, '{broken');
  assert.equal(app.host.bindHTTP('https://gateway.test/api').has('Authorization'), false);
  app.storage.getItem = () => { throw new Error('unavailable'); };
  assert.equal(app.host.bindHTTP('https://gateway.test/api').has('Authorization'), false);
  assert.equal(app.host.getError(), null);
});

test('literal nested fields and optional path/prefix resolve without expression parsing', async () => {
  const app = await setup([{ type: 'http', source: { key: 'record', path: ['a.b', 'credential'] }, target: { location: 'header', name: 'X-Value' } }]);
  await app.set({ 'a.b': { credential: 'literal' } });
  assert.equal(app.host.bindHTTP('https://gateway.test/api').get('X-Value'), 'literal');
  app.host.configure(declarations([{ type: 'http', source: { key: 'record' }, target: { location: 'header', name: 'X-Value' } }]));
  await app.set('whole');
  assert.equal(app.host.bindHTTP('https://gateway.test/api').get('X-Value'), 'whole');
});

test('activation rejects transport/schema conflicts and never keeps old bindings after an invalid replacement', async () => {
  for (const bindings of [[http, { ...http, target: { location: 'header', name: 'authorization' } }],
    [websocket, { ...websocket, target: { location: 'message', path: ['request_context', 'headers'] } }],
    [{ ...http, target: { location: 'query', name: 'token' } }],
    [{ ...http, target: { location: 'header', name: 'Cookie' } }],
    [{ ...websocket, target: { location: 'message', path: ['params', 'credential'] } }],
    [{ ...websocket, target: { location: 'message', path: ['request_context', '__proto__'] } }],
    [{ ...websocket, target: { location: 'message', path: ['request_context'] } }]]) {
    const app = await setup();
    assert.throws(() => app.host.configure(declarations(bindings)));
    assert.throws(() => app.host.bindHTTP('https://gateway.test/api'), /CONFIGURATION_ERROR/);
  }
});

test('WebSocket extension keys are plugin-defined without a host-reserved stamp', async () => {
  const app = await setup([
    { ...websocket, target: { location: 'message', path: ['request_context', 'tenantId'] } },
    { ...websocket, target: { location: 'message', path: ['request_context', 'context_stamp'] } },
  ]);
  await app.set({ credential: 'plugin-value' });
  assert.deepEqual(app.host.bindWebSocket({ method: 'config.get' }).request_context,
    { tenantId: 'plugin-value', context_stamp: 'plugin-value' });
});

test('context change waits locally for its control request terminal without an execution receipt', async () => {
  let finish;
  const app = await setup([http], { waitForInvocation: () => new Promise((resolve) => { finish = resolve; }) });
  const phases = []; app.host.subscribe((change) => phases.push(change));
  assert.equal(app.host.acceptOperation(operation({ credential: 'new' })), true);
  assert.equal(app.storage.data.size, 1);
  assert.equal(app.host.isChanging(), true);
  assert.deepEqual(phases.map((item) => item.phase), ['changing']);
  const frame = { target: { package_id: 'pkg.one' }, activation_generation: 'generation-1' };
  assert.equal(app.host.acceptsFrame(frame), true);
  assert.equal(app.host.acceptsFrame({ ...frame, activation_generation: 'old' }), false);
  assert.equal(app.host.acceptsFrame({ ...frame, target: { package_id: 'other' } }), false);
  finish(); await Promise.resolve();
  assert.equal(app.host.isChanging(), false);
  assert.deepEqual(phases.map((item) => item.phase), ['changing', 'ready']);
});

test('same-value writes and unrelated keys do not interrupt the request context', async () => {
  const app = await setup([http]); await app.set({ credential: 'same' });
  const changes = []; app.host.subscribeStorage((change) => changes.push(change));
  const epoch = app.host.getEpoch();
  await app.set({ credential: 'same' });
  assert.equal(app.host.getEpoch(), epoch);
  app.host.acceptOperation(operation(true, { params: { key: 'preference', value: true } }));
  assert.deepEqual(changes, [{ package_id: 'pkg.one', key: 'preference' }]);
  assert.equal(app.host.getEpoch(), epoch);
  const key = [...app.storage.data.keys()][0];
  app.storage.data.set(key, JSON.stringify({ credential: 'other-tab' }));
  app.host.storageChanged({ key }); await Promise.resolve();
  assert.equal(app.host.bindHTTP('https://gateway.test/api').get('Authorization'), 'Bearer other-tab');
  assert.equal(app.host.getEpoch(), epoch + 1);
});

test('a closed connection revokes active declarations until a fresh connection snapshot is injected', async () => {
  const app = await setup(); await app.set({ credential: 'persistent' });
  app.host.suspendConnection();
  assert.equal(app.host.isChanging(), true);
  assert.equal(app.host.isUnavailable(), true);
  assert.equal(app.host.acceptOperation(operation('late')), false);
  assert.throws(() => app.host.bindWebSocket({ method: 'config.get' }), /CLIENT_DECLARATIONS_REQUIRED/);
  assert.equal(app.host.acceptsFrame({ target: { package_id: 'pkg.one' }, activation_generation: 'generation-1' }), false);
  const next = declarations(); next.connectionEpoch = 'connection-2';
  app.host.configure(next); await Promise.resolve();
  assert.equal(app.host.isUnavailable(), false);
  assert.equal(app.host.bindWebSocket({ method: 'config.get' }).request_context.headers.Authorization, 'Bearer persistent');
  assert.equal(app.host.acceptOperation(operation({ credential: 'persistent' })), false);
  assert.equal(app.host.acceptOperation(operation({ credential: 'persistent' }, { connection_epoch: 'connection-2' })), true);
});

const server = createServer((request, response) => {
  if (request.url === '/stream') {
    response.writeHead(200, { 'Content-Type': 'text/event-stream', Connection: 'close' });
    response.write('data: first\n\n'); return;
  }
  if (request.url === '/redirect') { response.writeHead(302, { Location: '/echo' }); response.end(); return; }
  response.writeHead(200, { 'Content-Type': 'application/json', Connection: 'close' });
  response.end(JSON.stringify({ headers: request.headers }));
});
server.listen(0, '127.0.0.1'); await once(server, 'listening');
const base = `http://127.0.0.1:${server.address().port}/`;
after(() => { server.closeAllConnections(); server.close(); });

test('real HTTP fetch sends declared headers, preserves native response metadata, and refuses credential redirects', async () => {
  const app = await setup([http], { gatewayUrl: () => base }); await app.set({ credential: 'network' });
  const boundFetch = createGatewayFetch(app.host);
  const response = await boundFetch(base + 'echo');
  assert.equal(response.status, 200);
  assert.equal(response.url, base + 'echo');
  assert.equal((await response.json()).headers.authorization, 'Bearer network');
  await assert.rejects(boundFetch(base + 'redirect'));
});

test('context changes abort an established HTTP/SSE body and reject old buffered reads', async () => {
  const app = await setup([http], { gatewayUrl: () => base }); await app.set({ credential: 'one' });
  const boundFetch = createGatewayFetch(app.host);
  const stream = await boundFetch(base + 'stream');
  const reader = stream.body.getReader();
  assert.equal((await reader.read()).done, false);
  const pending = reader.read();
  await app.set({ credential: 'two' });
  await assert.rejects(pending);
  const buffered = await boundFetch(base + 'echo');
  await app.set({ credential: 'three' });
  await assert.rejects(buffered.json());
});

test('published file bodies survive credentials and capability changes while honoring caller cancellation', async () => {
  for (const change of ['credentials', 'activation', 'caller']) {
    const app = await setup([http], { gatewayUrl: () => base });
    await app.set({ credential: 'one' });
    const controller = new AbortController();
    const response = await createGatewayFetch(app.host)(base + 'extension-assets/pkg.one/generation-1/background.svg',
      { signal: controller.signal });
    await app.set({ credential: 'two' });
    if (change === 'activation') {
      const next = declarations([http]); next.packages[0].activationGeneration = 'new'; app.host.configure(next);
    } else if (change === 'caller') controller.abort();
    if (change !== 'caller') assert.equal((await response.json()).headers.authorization, undefined);
    else await assert.rejects(response.json(), { name: 'AbortError' });
  }
});

for (const [name, client, route] of [['main', webClient, '/ws'], ['Git', gitWatchClient, '/ws/git']]) {
  test(`${name} WebSocket cancels old requests and accepts business events without a context stamp`, async () => {
    const originalWindow = globalThis.window;
    const originalWebSocket = globalThis.WebSocket;
    class Socket {
      static OPEN = 1;
      static instance;
      readyState = 0;
      pendingSent;
      listeners = new Map();
      constructor(url) {
        this.url = url; Socket.instance = this;
        queueMicrotask(() => { this.readyState = Socket.OPEN; this.onopen(); });
      }
      nextRequest() { return new Promise((resolve) => { this.pendingSent = resolve; }); }
      addEventListener(event, handler) {
        const handlers = this.listeners.get(event) ?? [];
        handlers.push(handler); this.listeners.set(event, handlers);
      }
      send(raw) { this.pendingSent(JSON.parse(raw)); }
      receive(message) { this.onmessage({ data: JSON.stringify(message) }); }
      close(code = 1000) {
        this.readyState = 3; this.onclose({ code });
        for (const handler of this.listeners.get('close') ?? []) handler({ code });
      }
    }
    globalThis.window = { location: { protocol: 'https:', host: 'gateway.test', href: 'https://gateway.test/' },
      localStorage: new Storage(), setTimeout, clearTimeout };
    globalThis.WebSocket = Socket;
    const host = browserClientCapabilities;
    try {
      await client.connect();
      const socket = Socket.instance;
      assert.equal(socket.url, `wss://gateway.test${route}`);
      let sent = socket.nextRequest();
      const legacy = client.request('project.git.diff_watch', { path: 'project' });
      const plain = await sent;
      assert.equal(plain.request_context, undefined);
      socket.receive({ type: 'res', id: plain.id, ok: true, payload: { watch_id: 'legacy' } });
      assert.deepEqual(await legacy, { watch_id: 'legacy' });

      host.configure(declarations([websocket])); await Promise.resolve();
      assert.equal(host.acceptOperation(operation({ credential: 'git-one' })), true); await Promise.resolve();
      sent = socket.nextRequest();
      const old = client.request('project.git.diff_watch', { path: 'project' });
      const oldResult = assert.rejects(old, (error) => error.code === 'CONTEXT_CHANGED');
      const bound = await sent;
      assert.deepEqual(bound.params, { path: 'project' });
      assert.deepEqual(bound.request_context, { headers: { Authorization: 'Bearer git-one' } });
      const events = [];
      const off = client.on('project.git.diff_changed', (event) => events.push(event));
      try {
        assert.equal(host.acceptOperation(operation({ credential: 'git-two' })), true);
        await oldResult; await Promise.resolve();
        sent = socket.nextRequest();
        let settled = false;
        const current = client.request('project.git.diff_watch', { path: 'project' }).then((result) => {
          settled = true; return result;
        });
        const currentMessage = await sent;
        assert.deepEqual(currentMessage.request_context, { headers: { Authorization: 'Bearer git-two' } });
        socket.receive({ type: 'res', id: bound.id, ok: true, payload: { watch_id: 'stale' } });
        await Promise.resolve();
        assert.equal(settled, false);
        socket.receive({ type: 'res', id: currentMessage.id, ok: true, payload: { watch_id: 'current' } });
        assert.deepEqual(await current, { watch_id: 'current' });
        socket.receive({ type: 'event', event: 'project.git.diff_changed', payload: { watch_id: 'current' } });
        assert.deepEqual(events.map((event) => event.payload), [{ watch_id: 'current' }]);
        host.suspendConnection();
        socket.receive({ type: 'event', event: 'project.git.diff_changed', payload: { watch_id: 'unavailable' } });
        assert.equal(events.length, 1);
      } finally { off(); }

      host.configure(declarations([http])); await Promise.resolve();
      sent = socket.nextRequest();
      const onlyHTTP = client.request('project.git.diff_watch', {});
      const message = await sent;
      assert.equal(message.request_context, undefined);
      socket.receive({ type: 'res', id: message.id, ok: true, payload: {} });
      await onlyHTTP;
    } finally {
      await client.disconnect(); host.invalidate(); await Promise.resolve();
      if (originalWindow === undefined) delete globalThis.window; else globalThis.window = originalWindow;
      globalThis.WebSocket = originalWebSocket;
    }
  });
}

test('bound HTTP honors Request manual redirects and RequestInit precedence', async () => {
  const app = await setup([http], { gatewayUrl: () => base }); await app.set({ credential: 'manual' });
  const boundFetch = createGatewayFetch(app.host);
  const response = await boundFetch(new Request(base + 'redirect', { redirect: 'manual' }));
  assert.equal(response.status, 302);
  await response.body?.cancel();
  const override = await boundFetch(new Request(base + 'redirect', { redirect: 'error' }), { redirect: 'manual' });
  assert.equal(override.status, 302);
  await override.body?.cancel();
  await assert.rejects(boundFetch(new Request(base + 'redirect', { redirect: 'manual' }), { redirect: 'error' }));
  await assert.rejects(boundFetch(new Request(base + 'redirect', { redirect: 'follow' })));
});

test('context and caller cancellation discard bytes already queued in the HTTP body wrapper', async () => {
  for (const cause of ['context', 'caller']) {
    const app = await setup([http], { gatewayUrl: () => base }); await app.set({ credential: 'old' });
    const controller = new AbortController();
    const response = await createGatewayFetch(app.host)(base + 'echo', { signal: controller.signal });
    const reader = response.body.getReader();
    await new Promise((resolve) => setImmediate(resolve));
    if (cause === 'context') await app.set({ credential: 'new' });
    else controller.abort();
    await assert.rejects(reader.read(), { name: 'AbortError' });
  }
});

const turn = () => new Promise((resolve) => setImmediate(resolve));
async function withPluginClient(run) {
  const originalWindow = globalThis.window;
  const originalWebSocket = globalThis.WebSocket;
  class Socket {
    static OPEN = 1;
    static instance;
    readyState = 0;
    sent = [];
    listeners = new Map();
    constructor() {
      Socket.instance = this;
      queueMicrotask(() => { this.readyState = Socket.OPEN; this.onopen(); });
    }
    addEventListener(event, handler) {
      const handlers = this.listeners.get(event) ?? [];
      handlers.push(handler); this.listeners.set(event, handlers);
    }
    send(raw) { this.sent.push(JSON.parse(raw)); }
    receive(message) { this.onmessage({ data: JSON.stringify(message) }); }
    close(code = 1000) {
      this.readyState = 3; this.onclose({ code });
      for (const handler of this.listeners.get('close') ?? []) handler({ code });
    }
  }
  globalThis.window = { location: { protocol: 'https:', host: 'gateway.test', href: 'https://gateway.test/' },
    localStorage: new Storage(), setTimeout, clearTimeout };
  globalThis.WebSocket = Socket;
  const host = browserClientCapabilities;
  try {
    await webClient.connect();
    host.configure(declarations([websocket])); await turn();
    await run(Socket.instance, host);
  } finally {
    host.setInvocationWaiter(() => Promise.resolve());
    await webClient.disconnect(); host.invalidate(); await turn();
    if (originalWindow === undefined) delete globalThis.window; else globalThis.window = originalWindow;
    globalThis.WebSocket = originalWebSocket;
  }
}

for (const [name, read, payload, expected] of [
  ['catalog', () => systemPluginApi.catalog('catalog'), { packages: [{ package_id: 'one' }] }, [{ package_id: 'one' }]],
  ['mine', () => systemPluginApi.catalog('mine'), { packages: [] }, []],
  ['show', () => systemPluginApi.show('pkg.one'), { package: { package_id: 'pkg.one' } }, { package_id: 'pkg.one' }],
]) {
  test(`plugin ${name} waits for ready and retries with a fresh request and credential`, async () => {
    await withPluginClient(async (socket, host) => {
      const pending = read(); await turn();
      const first = socket.sent[0];
      let finish;
      host.setInvocationWaiter(() => new Promise((resolve) => { finish = resolve; }));
      assert.equal(host.acceptOperation(operation({ credential: 'new' })), true);
      await turn();
      assert.equal(socket.sent.length, 1, 'no retry while the context is changing');
      finish(); await turn();
      assert.equal(socket.sent.length, 2);
      const retry = socket.sent[1];
      assert.notEqual(retry.id, first.id);
      assert.equal(retry.method, first.method);
      assert.deepEqual(retry.params, first.params);
      assert.deepEqual(retry.request_context, { headers: { Authorization: 'Bearer new' } });
      socket.receive({ type: 'res', id: retry.id, ok: true, payload });
      assert.deepEqual(await pending, expected);
    });
  });
}

test('plugin reads retry at most once and preserve ordinary errors', async () => {
  await withPluginClient(async (socket, host) => {
    const pending = systemPluginApi.show('pkg.one');
    const rejected = assert.rejects(pending, (error) => error.code === 'CONTEXT_CHANGED');
    await turn(); host.configure(declarations([websocket])); await turn();
    assert.equal(socket.sent.length, 2);
    host.configure(declarations([websocket])); await rejected; await turn();
    assert.equal(socket.sent.length, 2);
    const denied = systemPluginApi.catalog('mine');
    const deniedResult = assert.rejects(denied, (error) => error.code === 'AUTH_REQUIRED');
    await turn();
    socket.receive({ type: 'res', id: socket.sent[2].id, ok: false,
      code: 'AUTH_REQUIRED', error: 'login required' });
    await deniedResult; await turn();
    assert.equal(socket.sent.length, 3);
  });
});

test('plugin context waits release subscriptions and timers on ready and timeout', async () => {
  await withPluginClient(async (socket, host) => {
    const subscribe = host.subscribe;
    const timers = new Map();
    let active = 0;
    host.subscribe = (listener) => {
      active++;
      const off = subscribe(listener);
      return () => { active--; off(); };
    };
    window.setTimeout = (callback) => { const id = Symbol(); timers.set(id, callback); return id; };
    window.clearTimeout = (id) => { timers.delete(id); };
    try {
      for (const timeout of [false, true]) {
        const pending = systemPluginApi.catalog('catalog');
        const rejected = timeout ? assert.rejects(pending, (error) => error.code === 'CONTEXT_CHANGED') : null;
        await turn();
        host.suspendConnection(); await turn();
        assert.equal(active, 1);
        assert.equal(timers.size, 1);
        if (timeout) {
          [...timers.values()][0](); await rejected;
        } else {
          host.configure(declarations([websocket])); await turn();
          socket.receive({ type: 'res', id: socket.sent.at(-1).id, ok: true, payload: { packages: [] } });
          assert.deepEqual(await pending, []);
        }
        assert.equal(active, 0);
        assert.equal(timers.size, 0);
      }
    } finally {
      host.subscribe = subscribe;
      window.setTimeout = setTimeout; window.clearTimeout = clearTimeout;
    }
  });
});

test('plugin reads do not retry with unavailable declarations', async () => {
  await withPluginClient(async (socket, host) => {
    const pending = systemPluginApi.show('pkg.one');
    const rejected = assert.rejects(pending, (error) => error.code === 'CONTEXT_CHANGED');
    await turn();
    assert.throws(() => host.configure({}));
    await rejected; await turn();
    assert.equal(socket.sent.length, 1);
  });
});

for (const [name, write] of [
  ['enable', () => systemPluginApi.setEnabled('pkg.one', true)],
  ['disable', () => systemPluginApi.setEnabled('pkg.one', false)],
  ['install', () => systemPluginApi.install('pkg.one')],
  ['import', () => systemPluginApi.importPath('/package')],
  ['uninstall', () => systemPluginApi.uninstall('pkg.one')],
  ['reload', () => systemPluginApi.reload('pkg.one')],
]) {
  test(`plugin ${name} is never replayed after context cancellation`, async () => {
    await withPluginClient(async (socket, host) => {
      const pending = write();
      const rejected = assert.rejects(pending, (error) => error.code === 'CONTEXT_CHANGED');
      await turn(); host.configure(declarations([websocket]));
      await rejected; await turn();
      assert.equal(socket.sent.length, 1);
    });
  });
}

const reloadOperation = (extra = {}) => operation(undefined, { method: 'page.reload', params: {}, ...extra });
async function setupReload(options = {}) {
  const app = await setup([], options);
  const snapshot = declarations([]);
  app.host.configure(snapshot); await Promise.resolve();
  return { ...app, snapshot };
}

test('page reload is explicit and never triggered by declarations or storage updates', async () => {
  let reloads = 0;
  const app = await setupReload({ reloadPage: () => { reloads++; } });
  await app.set({ credential: 'new' });
  app.host.acceptOperation(operation(undefined, { method: 'local_storage.delete', params: { key: 'record' } }));
  app.host.configure(app.snapshot);
  await turn();
  assert.equal(reloads, 0);
  assert.equal(app.host.acceptOperation(reloadOperation()), true);
  await turn();
  assert.equal(reloads, 1);
});

test('page reload runs immediately after storage without waiting for the invocation', async () => {
  let reloads = 0; let stored; let waits = 0;
  const app = await setupReload({ waitForInvocation: () => { waits++; return new Promise(() => {}); },
    reloadPage: () => { reloads++; stored = [...app.storage.data.values()]; } });
  await app.set({ credential: 'persisted' });
  assert.equal(app.host.acceptOperation(reloadOperation()), true);
  assert.equal(reloads, 1);
  assert.equal(waits, 0);
  assert.deepEqual(stored, [JSON.stringify({ credential: 'persisted' })]);
});

test('page reload requires an available host action, empty parameters and current ownership', async () => {
  let reloads = 0;
  const app = await setupReload({ reloadPage: () => { reloads++; } });
  for (const extra of [{ package_id: 'unknown' }, { activation_generation: 'old' }, { connection_epoch: 'old' },
    { params: { key: 'record' } }, { deadline: Date.now() - 1 }, { extra: true }]) {
    assert.equal(app.host.acceptOperation(reloadOperation(extra)), false);
  }
  const noAction = await setupReload();
  assert.equal(noAction.host.acceptOperation(reloadOperation()), false);
  assert.equal(noAction.host.getError(), 'CAPABILITY_UNAVAILABLE');
  await turn(); assert.equal(reloads, 0);
});

test('page reload host failures are reported locally without retrying', async () => {
  let attempts = 0;
  const app = await setupReload({ reloadPage: () => { attempts++; throw new Error('reload failed'); } });
  assert.equal(app.host.acceptOperation(reloadOperation()), false);
  await turn();
  assert.equal(attempts, 1);
  assert.equal(app.host.getError(), 'PAGE_RELOAD_FAILED');
});
