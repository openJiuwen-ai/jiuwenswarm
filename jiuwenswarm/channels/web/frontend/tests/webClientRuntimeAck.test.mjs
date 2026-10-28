// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

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
    this.sentRequests = [];
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
    this.sentRequests.push(this.lastRequest);
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
const { attachTeamSession, resetTeamAttachments } = await import(
  '../node_modules/.cache/web-client-runtime-ack/teamAttach.mjs'
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

test('request without a deadline accepts delayed receipts and remains abortable', async () => {
  const pending = webClient.request('video.qwen.tool', {}, { timeoutMs: 0 });
  await Promise.resolve();
  const id = FakeWebSocket.instance.lastRequest.id;
  await new Promise(resolve => setTimeout(resolve, 20));
  gatewayResponse(id);
  assert.deepEqual(await pending, { accepted: true });

  const controller = new AbortController();
  const aborted = webClient.request('video.qwen.tool', {}, { timeoutMs: 0, signal: controller.signal });
  await Promise.resolve();
  controller.abort();
  await assert.rejects(aborted, error => error.code === 'REQUEST_ABORTED');
});

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

test('team attachment is fire-and-forget and resets only for reconnect', async () => {
  await webClient.connect();
  resetTeamAttachments();
  const socket = FakeWebSocket.instance;
  const initialCount = socket.sentRequests.length;

  await attachTeamSession('team-session-1', 'team.work.normal');
  assert.equal(socket.sentRequests.length, initialCount + 1);
  assert.deepEqual(socket.lastRequest.params, {
    session_id: 'team-session-1',
    query: '',
    mode: 'team.work.normal',
    attach_only: true,
  });
  assert.equal(socket.lastRequest.is_stream, true);

  await attachTeamSession('team-session-1', 'team.work.normal');
  assert.equal(socket.sentRequests.length, initialCount + 1);

  resetTeamAttachments();
  await attachTeamSession('team-session-1', 'team.work.normal');
  assert.equal(socket.sentRequests.length, initialCount + 2);
});
