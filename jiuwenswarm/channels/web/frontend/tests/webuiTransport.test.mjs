import test from 'node:test';
import assert from 'node:assert/strict';

import {
  createMockTransport,
  createWebClientTransport,
  mapWebClientState,
} from '../node_modules/.cache/webui-transport/transport/index.js';

test('mock transport delivers injected frames and connection changes', () => {
  const transport = createMockTransport();
  const received = [];
  const states = [];
  const off = transport.subscribe({
    onRenderFrame: (payload) => received.push(['render', payload]),
    onClearFrame: (payload) => received.push(['clear', payload]),
    onErrorFrame: (payload) => received.push(['error', payload]),
    onConnectionStateChange: (state) => states.push(state),
  });

  transport.injectRenderFrame({ revision: 1 });
  transport.injectClearFrame({ revision: 2 });
  transport.injectErrorFrame({ code: 'SCHEMA_ERROR' });
  transport.setConnectionState('disconnected');
  assert.deepEqual(received, [
    ['render', { revision: 1 }],
    ['clear', { revision: 2 }],
    ['error', { code: 'SCHEMA_ERROR' }],
  ]);
  assert.deepEqual(states, ['disconnected']);

  off();
  transport.injectRenderFrame({ revision: 3 });
  assert.equal(received.length, 3);
});

test('mock transport records dispatch requests and can be synchronous or deferred', async () => {
  const transport = createMockTransport();
  const receipt = await transport.dispatch({
    event: 'ui.click',
    target: { sessionId: 's', slotId: 'slot', packageId: 'p', instanceKey: 'i' },
    payload: { action_id: 'a' },
  });
  assert.deepEqual(receipt, { accepted: true, status: 'handled' });
  assert.equal(transport.dispatches.length, 1);

  const deferred = createMockTransport({ autoAccept: false });
  const pending = deferred.dispatch({
    event: 'ui.click',
    target: { sessionId: 's', slotId: 'slot', packageId: 'p', instanceKey: 'i' },
    payload: {},
  });
  assert.equal(deferred.pendingDispatchCount, 1);
  deferred.resolveNextDispatch({ accepted: false, status: 'inactive', code: 'TARGET_INACTIVE' });
  assert.deepEqual(await pending, { accepted: false, status: 'inactive', code: 'TARGET_INACTIVE' });
});

test('web client state is mapped to the transport three-state model', () => {
  assert.equal(mapWebClientState('ready'), 'connected');
  assert.equal(mapWebClientState('idle'), 'idle');
  assert.equal(mapWebClientState('connecting'), 'disconnected');
  assert.equal(mapWebClientState('closed'), 'disconnected');
});

function createFakeClient(initialState = 'ready') {
  const handlers = new Map();
  const requests = [];
  let state = initialState;
  let stateHandler = null;
  let offCalls = 0;

  return {
    requests,
    get offCalls() {
      return offCalls;
    },
    on(eventName, handler) {
      const set = handlers.get(eventName) ?? new Set();
      set.add(handler);
      handlers.set(eventName, set);
      return () => {
        set.delete(handler);
        offCalls += 1;
      };
    },
    onStateChange(handler) {
      stateHandler = handler;
      return () => {
        stateHandler = null;
        offCalls += 1;
      };
    },
    getState() {
      return state;
    },
    async request(method, params) {
      requests.push({ method, params });
      return { accepted: true, status: 'handled' };
    },
    emit(eventName, payload) {
      for (const handler of handlers.get(eventName) ?? []) {
        handler({ payload });
      }
    },
    changeState(next) {
      state = next;
      if (stateHandler) {
        stateHandler(next);
      }
    },
  };
}

test('web client transport subscribes to the three frames and connection state', () => {
  const client = createFakeClient();
  const transport = createWebClientTransport(client);
  const received = [];
  const states = [];
  const off = transport.subscribe({
    onRenderFrame: (payload) => received.push(['render', payload]),
    onClearFrame: (payload) => received.push(['clear', payload]),
    onErrorFrame: (payload) => received.push(['error', payload]),
    onConnectionStateChange: (state) => states.push(state),
  });

  client.emit('webui.render', { revision: 1 });
  client.emit('webui.render.clear', { revision: 2 });
  client.emit('webui.render.error', { code: 'SCHEMA_ERROR' });
  client.changeState('connecting');
  client.changeState('ready');

  assert.deepEqual(received, [
    ['render', { revision: 1 }],
    ['clear', { revision: 2 }],
    ['error', { code: 'SCHEMA_ERROR' }],
  ]);
  assert.deepEqual(states, ['disconnected', 'connected']);
  assert.equal(transport.getConnectionState(), 'connected');

  off();
  assert.equal(client.offCalls, 4);
  client.emit('webui.render', { revision: 3 });
  assert.equal(received.length, 3);
});

test('web client transport builds the dispatch request and parses the receipt', async () => {
  const client = createFakeClient();
  const transport = createWebClientTransport(client);
  const receipt = await transport.dispatch({
    event: 'ui.change',
    target: { sessionId: 'sess', slotId: 'slot', packageId: 'pkg', instanceKey: 'inst' },
    payload: { action_id: 'a', args: {}, data: { value: 'x' }, node_key: '0', revision: 1 },
  });

  assert.deepEqual(receipt, { accepted: true, status: 'handled' });
  assert.equal(client.requests.length, 1);
  assert.deepEqual(client.requests[0], {
    method: 'webui.dispatch',
    params: {
      event: 'ui.change',
      session_id: 'sess',
      target: { slot_id: 'slot', package_id: 'pkg', instance_key: 'inst' },
      payload: { action_id: 'a', args: {}, data: { value: 'x' }, node_key: '0', revision: 1 },
    },
  });
});

test('trusted client declarations configure the capability host and unsubscribe with the transport', () => {
  const client = createFakeClient();
  const calls = [];
  const capabilities = {
    configure: (snapshot) => { calls.push(['configure', snapshot]); },
    acceptOperation: (operation) => { calls.push(['operation', operation]); },
    acceptsFrame: () => true,
  };
  const transport = createWebClientTransport(client, capabilities);
  const off = transport.subscribe({
    onRenderFrame: (frame) => { calls.push(['render', frame]); },
    onClearFrame: () => undefined,
    onErrorFrame: () => undefined,
    onConnectionStateChange: () => undefined,
  });
  const snapshot = { clientProtocolVersion: 1, gatewayInstanceId: 'gateway', connectionEpoch: 'socket', packages: [] };
  client.emit('client.declarations', snapshot);
  client.emit('webui.render', { revision: 1 });
  assert.deepEqual(calls, [['configure', snapshot], ['render', { revision: 1 }]]);
  capabilities.configure = () => { throw new Error('configuration rejected'); };
  assert.doesNotThrow(() => client.emit('client.declarations', {}));
  off();
  assert.equal(client.offCalls, 6);
  client.emit('client.declarations', snapshot);
  client.emit('client.operation', {});
  assert.equal(calls.length, 2);
});

test('web client transport returns null for failures and malformed receipts', async () => {
  const failing = createFakeClient();
  failing.request = async () => {
    throw new Error('offline');
  };
  const failingTransport = createWebClientTransport(failing);
  assert.equal(
    await failingTransport.dispatch({
      event: 'ui.click',
      target: { sessionId: 's', slotId: 'slot', packageId: 'p', instanceKey: 'i' },
      payload: {},
    }),
    null,
  );

  const malformed = createFakeClient();
  malformed.request = async () => ({ ok: true });
  const malformedTransport = createWebClientTransport(malformed);
  assert.equal(
    await malformedTransport.dispatch({
      event: 'ui.click',
      target: { sessionId: 's', slotId: 'slot', packageId: 'p', instanceKey: 'i' },
      payload: {},
    }),
    null,
  );
});
