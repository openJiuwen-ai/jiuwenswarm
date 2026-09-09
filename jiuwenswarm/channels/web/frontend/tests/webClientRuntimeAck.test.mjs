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
