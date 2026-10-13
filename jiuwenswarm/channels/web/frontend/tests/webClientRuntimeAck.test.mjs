import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.window = globalThis;
globalThis.window.location = { protocol: 'http:', host: 'localhost:18180' };

class FakeWebSocket {
  static OPEN = 1;
  static instance;

  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.listeners = new Map();
    FakeWebSocket.instance = this;
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.();
    });
  }

  addEventListener(event, handler) {
    const handlers = this.listeners.get(event) ?? [];
    handlers.push(handler);
    this.listeners.set(event, handlers);
  }

  send(raw) {
    this.lastRequest = JSON.parse(raw);
  }

  close(code = 1000, reason = '') {
    this.readyState = 3;
    const event = { code, reason, wasClean: code === 1000 };
    this.onclose?.(event);
    for (const handler of this.listeners.get('close') ?? []) handler(event);
  }

  receive(message) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
}

globalThis.WebSocket = FakeWebSocket;

const { webClient } = await import(
  '../node_modules/.cache/web-client-runtime-ack/webClient.mjs'
);

await webClient.connect();

function gatewayResponse(id, ok = true) {
  FakeWebSocket.instance.receive({ type: 'res', id, ok, payload: { accepted: ok } });
}

function runtimeEvent(event, requestId, payload = {}) {
  FakeWebSocket.instance.receive({
    type: 'event',
    event,
    payload: { request_id: requestId, ...payload },
  });
}

test('runtime-ack request stays pending after gateway response and resolves exact ack', async () => {
  let settled = false;
  const result = webClient.request(
    'chat.send',
    { source: 'permission_interrupt' },
    { awaitRuntimeAccepted: true, timeoutMs: 1000 },
  ).finally(() => {
    settled = true;
  });
  await Promise.resolve();
  const requestId = FakeWebSocket.instance.lastRequest.id;

  gatewayResponse(requestId);
  runtimeEvent('runtime.accepted', 'other-request');
  await Promise.resolve();
  assert.equal(settled, false);

  runtimeEvent('runtime.accepted', requestId, { accepted: true });
  assert.deepEqual(await result, { request_id: requestId, accepted: true });
});

test('runtime-ack request rejects exact Host error while ordinary request keeps gateway semantics', async () => {
  const permission = webClient.request(
    'chat.send',
    { source: 'permission_interrupt' },
    { awaitRuntimeAccepted: true, timeoutMs: 1000 },
  );
  await Promise.resolve();
  const permissionId = FakeWebSocket.instance.lastRequest.id;
  gatewayResponse(permissionId);
  runtimeEvent('chat.error', permissionId, { error: 'send failed' });
  await assert.rejects(permission, /send failed/);

  const ordinary = webClient.request('chat.send', { source: 'ask_user_interrupt' });
  await Promise.resolve();
  const ordinaryId = FakeWebSocket.instance.lastRequest.id;
  gatewayResponse(ordinaryId);
  assert.deepEqual(await ordinary, { accepted: true });
});

test('abort keeps runtime-ack request rejected without waiting for Host event', async () => {
  const controller = new AbortController();
  const pending = webClient.request(
    'chat.send',
    { source: 'permission_interrupt' },
    {
      awaitRuntimeAccepted: true,
      timeoutMs: 1000,
      signal: controller.signal,
    },
  );
  await Promise.resolve();
  const requestId = FakeWebSocket.instance.lastRequest.id;
  gatewayResponse(requestId);
  controller.abort();

  await assert.rejects(pending, (error) => error.code === 'REQUEST_ABORTED');
  runtimeEvent('runtime.accepted', requestId);
  await Promise.resolve();
});

test('request identity is bound before send and runtime errors preserve their code and payload', async () => {
  let boundId;
  const socket = FakeWebSocket.instance;
  const originalSend = socket.send;
  socket.send = function (raw) {
    const outgoing = JSON.parse(raw);
    assert.equal(boundId, outgoing.id);
    originalSend.call(this, raw);
  };
  try {
    const pending = webClient.request('chat.send', { input_mode: 'steer' }, {
      awaitRuntimeAccepted: true,
      onRequestId: (id) => { boundId = id; },
    });
    await Promise.resolve();
    runtimeEvent('chat.error', boundId, { code: 'SESSION_INPUT_DELIVERY_UNKNOWN', error: 'uncertain' });
    await assert.rejects(pending, (error) => {
      assert.equal(error.code, 'SESSION_INPUT_DELIVERY_UNKNOWN');
      assert.equal(error.payload.error, 'uncertain');
      assert.equal(error.requestId, boundId);
      return true;
    });
  } finally { socket.send = originalSend; }
});

test('legacy content wrapping an acceptance is routed as ACK, not final output', async () => {
  let finalCount = 0;
  const unsubscribe = webClient.on('chat.final', () => { finalCount += 1; });
  try {
    const pending = webClient.request('chat.send', { input_mode: 'steer' }, { awaitRuntimeAccepted: true });
    await Promise.resolve();
    const requestId = FakeWebSocket.instance.lastRequest.id;
    FakeWebSocket.instance.receive({ type: 'content', payload: { event_type: 'runtime.accepted', request_id: requestId } });
    assert.equal((await pending).request_id, requestId);
    assert.equal(finalCount, 0);
  } finally { unsubscribe(); }
});

test('timeout and disconnect reject runtime-ack requests after gateway acceptance', async () => {
  const timedOut = webClient.request(
    'chat.send',
    { source: 'permission_interrupt' },
    { awaitRuntimeAccepted: true, timeoutMs: 5 },
  );
  await Promise.resolve();
  gatewayResponse(FakeWebSocket.instance.lastRequest.id);
  await assert.rejects(timedOut, (error) => error.code === 'REQUEST_TIMEOUT');

  const disconnected = webClient.request(
    'chat.send',
    { source: 'permission_interrupt' },
    { awaitRuntimeAccepted: true, timeoutMs: 1000 },
  );
  await Promise.resolve();
  gatewayResponse(FakeWebSocket.instance.lastRequest.id);
  const closed = webClient.disconnect();
  await assert.rejects(disconnected, (error) => error.code === 'WS_CLOSED');
  await closed;
});

test('reconnect resumes the original event cursor without resubmitting input', async () => {
  await webClient.connect();
  let deltas = 0;
  let processing;
  const offDelta = webClient.on('chat.delta', () => { deltas += 1; });
  const offStatus = webClient.on('chat.processing_status', (event) => { processing = event.payload.is_processing; });
  const payload = { session_id: 'resume-session', content: 'original-output',
    runtime_event: { worker_instance: 'worker-original', sequence: 17, dispatch_id: 'original-dispatch' } };
  try {
    runtimeEvent('chat.delta', 'original-request', payload);
    runtimeEvent('chat.delta', 'original-request', payload);
    assert.equal(deltas, 1);
    await webClient.disconnect();
    await webClient.connect();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const query = FakeWebSocket.instance.lastRequest;
    assert.equal(query.method, 'session.get_metadata');
    assert.deepEqual(query.params, { session_id: 'resume-session',
      runtime_resume: { worker_instance: 'worker-original', after: 17 } });
    FakeWebSocket.instance.receive({ type: 'res', id: query.id, ok: true,
      payload: { runtime: { is_processing: false, history_required: false } } });
    await new Promise((resolve) => setTimeout(resolve, 0));
    runtimeEvent('chat.delta', 'original-request', payload);
    runtimeEvent('chat.delta', 'original-request', { ...payload,
      runtime_event: { ...payload.runtime_event, sequence: 18, replayed: true } });
    assert.equal(deltas, 2);
    assert.equal(processing, false);
  } finally {
    offDelta(); offStatus();
    await webClient.disconnect();
  }
});

test('a fresh page subscribes to the execution discovered in session metadata', async () => {
  const fresh = new webClient.constructor();
  await fresh.connect();
  const socket = FakeWebSocket.instance;
  const sent = [];
  const originalSend = socket.send;
  socket.send = function (raw) {
    sent.push(JSON.parse(raw));
    originalSend.call(this, raw);
  };
  let processing;
  const off = fresh.on('chat.processing_status', (event) => { processing = event.payload.is_processing; });
  try {
    const pending = fresh.request('session.get_metadata', { session_id: 'fresh-session' });
    await Promise.resolve();
    socket.receive({ type: 'res', id: socket.lastRequest.id, ok: true, payload: {
      runtime: { worker_instance: 'existing-worker', is_processing: true },
    } });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const subscription = socket.lastRequest;
    assert.deepEqual(subscription.params, { session_id: 'fresh-session',
      runtime_resume: { worker_instance: 'existing-worker', after: 0 } });
    socket.receive({ type: 'res', id: subscription.id, ok: true, payload: {
      runtime: { worker_instance: 'existing-worker', is_processing: true, history_required: false },
    } });
    await pending;
    assert.equal(processing, true);
    assert.deepEqual(sent.map((request) => request.method), ['session.get_metadata', 'session.get_metadata']);
  } finally {
    off();
    await fresh.disconnect();
  }
});
