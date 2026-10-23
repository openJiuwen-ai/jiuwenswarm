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

const { webClient } = await import('../node_modules/.cache/web-client-runtime-ack/webClient.mjs');
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

test('runtime-ack request stays pending after gateway response and resolves exact ack', async () => {
  let settled = false;
  const result = webClient
    .request('chat.send', { source: 'permission_interrupt' }, { awaitRuntimeAccepted: true, timeoutMs: 1000 })
    .finally(() => {
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
    const pending = webClient.request(
      'chat.send',
      { input_mode: 'steer' },
      {
        awaitRuntimeAccepted: true,
        onRequestId: (id) => {
          boundId = id;
        },
      },
    );
    await Promise.resolve();
    runtimeEvent('chat.error', boundId, { code: 'SESSION_INPUT_DELIVERY_UNKNOWN', error: 'uncertain' });
    await assert.rejects(pending, (error) => {
      assert.equal(error.code, 'SESSION_INPUT_DELIVERY_UNKNOWN');
      assert.equal(error.payload.error, 'uncertain');
      assert.equal(error.requestId, boundId);
      return true;
    });
  } finally {
    socket.send = originalSend;
  }
});

test('legacy content wrapping an acceptance is routed as ACK, not final output', async () => {
  let finalCount = 0;
  const unsubscribe = webClient.on('chat.final', () => {
    finalCount += 1;
  });
  try {
    const pending = webClient.request('chat.send', { input_mode: 'steer' }, { awaitRuntimeAccepted: true });
    await Promise.resolve();
    const requestId = FakeWebSocket.instance.lastRequest.id;
    FakeWebSocket.instance.receive({
      type: 'content',
      payload: { event_type: 'runtime.accepted', request_id: requestId },
    });
    assert.equal((await pending).request_id, requestId);
    assert.equal(finalCount, 0);
  } finally {
    unsubscribe();
  }
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
  const offDelta = webClient.on('chat.delta', () => {
    deltas += 1;
  });
  const offStatus = webClient.on('chat.processing_status', (event) => {
    processing = event.payload.is_processing;
  });
  const payload = {
    session_id: 'resume-session',
    content: 'original-output',
    runtime_event: { worker_instance: 'worker-original', sequence: 17, dispatch_id: 'original-dispatch' },
  };
  try {
    runtimeEvent('chat.delta', 'original-request', payload);
    runtimeEvent('chat.delta', 'original-request', payload);
    assert.equal(deltas, 1);
    await webClient.disconnect();
    await webClient.connect();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const query = FakeWebSocket.instance.lastRequest;
    assert.equal(query.method, 'session.get_metadata');
    assert.deepEqual(query.params, {
      session_id: 'resume-session',
      runtime_resume: { worker_instance: 'worker-original', after: 17 },
    });
    FakeWebSocket.instance.receive({
      type: 'res',
      id: query.id,
      ok: true,
      payload: { runtime: { is_processing: false, history_required: false } },
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    runtimeEvent('chat.delta', 'original-request', payload);
    runtimeEvent('chat.delta', 'original-request', {
      ...payload,
      runtime_event: { ...payload.runtime_event, sequence: 18, replayed: true },
    });
    assert.equal(deltas, 2);
    assert.equal(processing, false);
  } finally {
    offDelta();
    offStatus();
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
  const off = fresh.on('chat.processing_status', (event) => {
    processing = event.payload.is_processing;
  });
  try {
    const pending = fresh.request('session.get_metadata', { session_id: 'fresh-session' });
    await Promise.resolve();
    socket.receive({
      type: 'res',
      id: socket.lastRequest.id,
      ok: true,
      payload: {
        runtime: { worker_instance: 'existing-worker', is_processing: true },
      },
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
    const subscription = socket.lastRequest;
    assert.deepEqual(subscription.params, {
      session_id: 'fresh-session',
      runtime_resume: { worker_instance: 'existing-worker', after: 0 },
    });
    socket.receive({
      type: 'res',
      id: subscription.id,
      ok: true,
      payload: {
        runtime: { worker_instance: 'existing-worker', is_processing: true, history_required: false },
      },
    });
    await pending;
    assert.equal(processing, true);
    assert.deepEqual(
      sent.map((request) => request.method),
      ['session.get_metadata', 'session.get_metadata'],
    );
  } finally {
    off();
    await fresh.disconnect();
  }
});

test('pending reconnect recovery preserves processing and retries only the original cursor with a bound', async () => {
  const fresh = new webClient.constructor();
  await fresh.connect();
  let processing = true;
  const snapshots = [];
  const sent = [];
  const offStatus = fresh.on('chat.processing_status', ({ payload }) => {
    processing = payload.is_processing;
  });
  const offSnapshot = fresh.on('runtime.snapshot', ({ payload }) => {
    snapshots.push(payload);
  });
  FakeWebSocket.instance.receive({
    type: 'event',
    event: 'chat.delta',
    payload: {
      session_id: 'pending-session',
      runtime_event: { worker_instance: 'original-worker', sequence: 27 },
    },
  });
  await fresh.disconnect();
  const originalSend = FakeWebSocket.prototype.send;
  FakeWebSocket.prototype.send = function (raw) {
    originalSend.call(this, raw);
    const query = JSON.parse(raw);
    sent.push(query);
    queueMicrotask(() =>
      this.receive({
        type: 'res',
        id: query.id,
        ok: true,
        payload: {
          runtime: { recovery_pending: true, recovery_reason: 'snapshot_timeout', retry_after_ms: 1 },
        },
      }),
    );
  };
  try {
    await fresh.connect();
    await new Promise((resolve) => setTimeout(resolve, 350));
    assert.equal(processing, true);
    assert.equal(snapshots.length, 3);
    assert.deepEqual(
      sent.map(({ method, params }) => ({ method, params })),
      Array.from({ length: 3 }, () => ({
        method: 'session.get_metadata',
        params: { session_id: 'pending-session', runtime_resume: { worker_instance: 'original-worker', after: 27 } },
      })),
    );
    assert.ok(snapshots.every((snapshot) => snapshot.recovery_pending === true));
  } finally {
    FakeWebSocket.prototype.send = originalSend;
    offStatus();
    offSnapshot();
    await fresh.disconnect();
  }
});

test('fresh metadata pending recovery reaches the later running snapshot without clearing processing', async () => {
  const fresh = new webClient.constructor();
  await fresh.connect();
  const socket = FakeWebSocket.instance;
  const originalSend = socket.send;
  const sent = [];
  let processing = true;
  const snapshots = [];
  const offStatus = fresh.on('chat.processing_status', ({ payload }) => {
    processing = payload.is_processing;
  });
  const offSnapshot = fresh.on('runtime.snapshot', ({ payload }) => {
    snapshots.push(payload);
    assert.equal(processing, true);
  });
  socket.send = function (raw) {
    originalSend.call(this, raw);
    const query = JSON.parse(raw);
    sent.push(query);
    const runtime =
      sent.length < 3
        ? { recovery_pending: true, worker_instance: 'existing-worker', retry_after_ms: 1 }
        : { is_processing: true, worker_instance: 'existing-worker', cursor: 2048 };
    queueMicrotask(() =>
      this.receive({ type: 'res', id: query.id, ok: true, payload: { is_processing: true, runtime } }),
    );
  };
  try {
    await fresh.request('session.get_metadata', { session_id: 'fresh-pending' });
    assert.equal(processing, true);
    assert.equal(snapshots.length, 2);
    assert.equal(snapshots[0].recovery_pending, true);
    assert.equal(snapshots[1].cursor, 2048);
    assert.deepEqual(
      sent.map(({ method }) => method),
      Array(3).fill('session.get_metadata'),
    );
    assert.deepEqual(
      sent.slice(1).map(({ params }) => params),
      Array.from({ length: 2 }, () => ({
        session_id: 'fresh-pending',
        runtime_resume: { worker_instance: 'existing-worker', after: 0 },
      })),
    );
  } finally {
    socket.send = originalSend;
    offStatus();
    offSnapshot();
    await fresh.disconnect();
  }
});

test('disconnect during pending recovery prevents another query on the replacement socket', async () => {
  const fresh = new webClient.constructor();
  await fresh.connect();
  FakeWebSocket.instance.receive({
    type: 'event',
    event: 'chat.delta',
    payload: {
      session_id: 'disconnect-session',
      runtime_event: { worker_instance: 'original-worker', sequence: 9 },
    },
  });
  await fresh.disconnect();
  const originalSend = FakeWebSocket.prototype.send;
  const sent = [];
  let releaseSnapshot;
  const snapshot = new Promise((resolve) => {
    releaseSnapshot = resolve;
  });
  const off = fresh.on('runtime.snapshot', releaseSnapshot);
  FakeWebSocket.prototype.send = function (raw) {
    originalSend.call(this, raw);
    const query = JSON.parse(raw);
    sent.push(query);
    queueMicrotask(() =>
      this.receive({
        type: 'res',
        id: query.id,
        ok: true,
        payload: {
          runtime: { recovery_pending: true, retry_after_ms: 1 },
        },
      }),
    );
  };
  try {
    await fresh.connect();
    await snapshot;
    await fresh.disconnect();
    await new Promise((resolve) => setTimeout(resolve, 150));
    assert.equal(sent.length, 1);
  } finally {
    FakeWebSocket.prototype.send = originalSend;
    off();
    await fresh.disconnect();
  }
});

test('late callbacks from a replaced socket preserve current requests, processing and the recovery cursor', async () => {
  const fresh = new webClient.constructor();
  await fresh.connect();
  const oldSocket = FakeWebSocket.instance;
  const oldClose = oldSocket.close;
  // Model a close handshake that finishes after disconnect's 800ms fallback.
  oldSocket.close = function () {
    this.readyState = 2;
  };
  await fresh.disconnect();
  await fresh.connect();
  const currentSocket = FakeWebSocket.instance;
  let processing = true;
  const off = fresh.on('chat.processing_status', ({ payload }) => {
    processing = payload.is_processing;
  });
  try {
    currentSocket.receive({
      type: 'event',
      event: 'chat.delta',
      payload: {
        session_id: 'socket-generation',
        runtime_event: { worker_instance: 'current-worker', sequence: 23 },
      },
    });
    const pending = fresh.request('config.get');
    await Promise.resolve();
    const currentRequest = currentSocket.lastRequest;
    oldSocket.onopen?.();
    oldSocket.receive({
      type: 'event',
      event: 'chat.processing_status',
      payload: {
        session_id: 'socket-generation',
        is_processing: false,
        runtime_event: { worker_instance: 'old-worker', sequence: 999 },
      },
    });
    oldSocket.onerror?.();
    oldClose.call(oldSocket);
    assert.equal(fresh.getState(), 'ready');
    assert.equal(processing, true);
    assert.equal(currentSocket.lastRequest.id, currentRequest.id);
    currentSocket.receive({ type: 'res', id: currentRequest.id, ok: true, payload: { current_socket: true } });
    assert.deepEqual(await pending, { current_socket: true });
    await fresh.disconnect();
    await fresh.connect();
    await new Promise((resolve) => setTimeout(resolve, 0));
    const resumed = FakeWebSocket.instance.lastRequest;
    assert.equal(resumed.method, 'session.get_metadata');
    assert.deepEqual(resumed.params, {
      session_id: 'socket-generation',
      runtime_resume: { worker_instance: 'current-worker', after: 23 },
    });
    FakeWebSocket.instance.receive({
      type: 'res',
      id: resumed.id,
      ok: true,
      payload: {
        runtime: { is_processing: true, worker_instance: 'current-worker' },
      },
    });
    await new Promise((resolve) => setTimeout(resolve, 0));
  } finally {
    off();
    await fresh.disconnect();
  }
});

test('replaced pending connections reject only their own promise on late lifecycle callbacks', async () => {
  for (const callback of ['onopen', 'onerror', 'onclose']) {
    const fresh = new webClient.constructor();
    const pendingConnect = fresh.connect();
    const rejected = assert.rejects(pendingConnect, (error) => error.code === 'WS_CLOSED');
    const oldSocket = FakeWebSocket.instance;
    const lateCallback = oldSocket[callback];
    oldSocket.onopen = undefined;
    await fresh.disconnect();
    await fresh.connect();
    try {
      lateCallback({ code: 1000, reason: 'replaced', wasClean: true });
      await rejected;
      assert.equal(fresh.getState(), 'ready', callback);
      const currentSocket = FakeWebSocket.instance;
      const pending = fresh.request('config.get');
      await Promise.resolve();
      currentSocket.receive({ type: 'res', id: currentSocket.lastRequest.id, ok: true, payload: { callback } });
      assert.deepEqual(await pending, { callback });
    } finally {
      await fresh.disconnect();
    }
  }
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
