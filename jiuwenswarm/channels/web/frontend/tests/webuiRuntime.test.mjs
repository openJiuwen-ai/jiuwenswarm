import test from 'node:test';
import assert from 'node:assert/strict';

import { createWebUIRuntime } from '../node_modules/.cache/webui-runtime/runtime/index.js';
import { createMockTransport } from '../node_modules/.cache/webui-runtime/transport/index.js';
import { DEMO_SLOT, DEMO_SLOT_SINGLE, demoCatalog } from '../node_modules/.cache/webui-runtime/testing/demoCatalog.js';
import {
  demoClearFrame,
  demoErrorFrame,
  demoRenderFrame,
} from '../node_modules/.cache/webui-runtime/testing/fixtures.js';

const INSTANCE_KEY = 'sess-1|conversation.panel.right|pkg.demo|main';

function setup(options = {}) {
  const transport = createMockTransport(options);
  const runtime = createWebUIRuntime({ transport, catalog: demoCatalog });
  runtime.start();
  return { transport, runtime };
}

async function flush() {
  await new Promise((resolve) => setTimeout(resolve, 0));
}

test('render frame commits a stable instance snapshot with display state', () => {
  const { transport, runtime } = setup();
  transport.injectRenderFrame(demoRenderFrame({ revision: 3 }));

  const instance = runtime.getInstance(INSTANCE_KEY);
  assert.equal(instance.state, 'active');
  assert.equal(instance.revision, 3);
  assert.equal(instance.displayedRevision, 0);
  assert.equal(instance.renderFailure, null);
  assert.equal(instance.frame.tree.component, 'demo');
  assert.deepEqual(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).instanceKeys, [INSTANCE_KEY]);
});

test('content updates notify the instance, membership updates notify the scope', () => {
  const { transport, runtime } = setup();
  let instanceCalls = 0;
  let scopeCalls = 0;
  runtime.subscribeInstance(INSTANCE_KEY, () => {
    instanceCalls += 1;
  });
  runtime.subscribeScope('sess-1', DEMO_SLOT, () => {
    scopeCalls += 1;
  });

  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  assert.equal(instanceCalls, 1);
  assert.equal(scopeCalls, 1);

  transport.injectRenderFrame(demoRenderFrame({ revision: 2 }));
  assert.equal(instanceCalls, 2);
  assert.equal(scopeCalls, 1, 'content update must not re-render the scope');

  transport.injectClearFrame(demoClearFrame({ revision: 3 }));
  assert.equal(instanceCalls, 3);
  assert.equal(scopeCalls, 2, 'membership removal notifies the scope');
});

test('stale frames and stale clears are ignored', () => {
  const { transport, runtime } = setup();
  transport.injectRenderFrame(demoRenderFrame({ revision: 2 }));
  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  assert.equal(runtime.getInstance(INSTANCE_KEY).revision, 2);

  transport.injectClearFrame(demoClearFrame({ revision: 1 }));
  assert.notEqual(runtime.getInstance(INSTANCE_KEY), undefined);
});

test('mount handles are independent and keep their own ready information', async () => {
  const { transport, runtime } = setup();
  const first = runtime.mountSlot('sess-1', DEMO_SLOT);
  const second = runtime.mountSlot('sess-1', DEMO_SLOT);
  assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, true);

  await first.notifyReady();
  await second.notifyReady();
  assert.equal(transport.dispatches.filter((request) => request.event === 'ui.ready').length, 2);

  first.release();
  assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, true, 'other consumer still owns the scope');

  transport.injectRenderFrame(demoRenderFrame());
  assert.notEqual(runtime.getInstance(INSTANCE_KEY), undefined);

  second.release();
  assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, false);
  assert.equal(runtime.getInstance(INSTANCE_KEY), undefined, 'last consumer release drops local instances');
});

test('reconnect resends ready per consumer', async () => {
  const { transport, runtime } = setup();
  const handle = runtime.mountSlot('sess-1', DEMO_SLOT);
  await handle.notifyReady();
  assert.equal(transport.dispatches.length, 1);

  transport.setConnectionState('disconnected');
  transport.setConnectionState('connected');
  await flush();
  assert.equal(transport.dispatches.filter((request) => request.event === 'ui.ready').length, 2);
  handle.release();
});

test('dispatch requires the current frame revision and a live connection', async () => {
  const { transport, runtime } = setup();
  transport.injectRenderFrame(demoRenderFrame({ revision: 5 }));

  const stale = await runtime.dispatchIntent({
    instanceKey: INSTANCE_KEY,
    nodeKey: '0.0',
    actionId: 'demo.refresh',
    frameRevision: 4,
  });
  assert.deepEqual(stale, { status: 'inactive' });

  const sent = await runtime.dispatchIntent({
    instanceKey: INSTANCE_KEY,
    nodeKey: '0.0',
    actionId: 'demo.refresh',
    frameRevision: 5,
  });
  assert.equal(sent.status, 'sent');
  assert.equal(transport.dispatches.at(-1).payload.action_id, 'demo.refresh');

  transport.setConnectionState('disconnected');
  const offline = await runtime.dispatchIntent({
    instanceKey: INSTANCE_KEY,
    nodeKey: '0.0',
    actionId: 'demo.refresh',
    frameRevision: 5,
  });
  assert.deepEqual(offline, { status: 'disconnected' });
});

test('duplicate in-flight interactions are ignored until the first completes', async () => {
  const { transport, runtime } = setup({ autoAccept: false });
  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  const first = runtime.dispatchIntent({
    instanceKey: INSTANCE_KEY,
    nodeKey: '0.0',
    actionId: 'demo.refresh',
    frameRevision: 1,
  });
  const duplicate = await runtime.dispatchIntent({
    instanceKey: INSTANCE_KEY,
    nodeKey: '0.0',
    actionId: 'demo.refresh',
    frameRevision: 1,
  });
  assert.deepEqual(duplicate, { status: 'duplicate' });
  transport.resolveNextDispatch({ accepted: true, status: 'handled' });
  assert.equal((await first).status, 'sent');
});

test('transport exceptions become a failed interaction result', async () => {
  const transport = createMockTransport();
  transport.dispatch = async () => {
    throw new Error('offline');
  };
  const runtime = createWebUIRuntime({ transport, catalog: demoCatalog });
  runtime.start();
  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  const result = await runtime.dispatchIntent({
    instanceKey: INSTANCE_KEY,
    nodeKey: '0.0',
    actionId: 'demo.refresh',
    frameRevision: 1,
  });
  assert.deepEqual(result, { status: 'failed', error: 'offline' });
});

test('display feedback and render failures are frame-scoped', () => {
  const { transport, runtime } = setup();
  transport.injectRenderFrame(demoRenderFrame({ revision: 2 }));
  const frameId = runtime.getInstance(INSTANCE_KEY).localFrameId;

  runtime.reportDisplayed(INSTANCE_KEY, 2, frameId);
  assert.equal(runtime.getInstance(INSTANCE_KEY).displayedRevision, 2);
  runtime.reportDisplayed(INSTANCE_KEY, 1, frameId);
  assert.equal(runtime.getInstance(INSTANCE_KEY).displayedRevision, 2, 'stale confirmation ignored');
  runtime.reportDisplayed(INSTANCE_KEY, 2, frameId - 1);
  assert.equal(runtime.getInstance(INSTANCE_KEY).displayedRevision, 2, 'confirmation from another generation ignored');

  runtime.reportRenderFailure(INSTANCE_KEY, 2, 'adapter crash', frameId);
  assert.deepEqual(runtime.getInstance(INSTANCE_KEY).renderFailure, {
    revision: 2,
    localFrameId: frameId,
    message: 'adapter crash',
  });
  runtime.reportRenderFailure(INSTANCE_KEY, 1, 'old failure', frameId);
  assert.equal(runtime.getInstance(INSTANCE_KEY).renderFailure.message, 'adapter crash');
  runtime.reportRenderFailure(INSTANCE_KEY, 2, 'old generation failure', frameId - 1);
  assert.equal(runtime.getInstance(INSTANCE_KEY).renderFailure.message, 'adapter crash');

  transport.injectRenderFrame(demoRenderFrame({ revision: 3 }));
  assert.equal(runtime.getInstance(INSTANCE_KEY).renderFailure, null, 'new frame clears old failure');
});

test('clear and rebuild with the same revision starts a new render generation', () => {
  const { transport, runtime } = setup();
  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  const first = runtime.getInstance(INSTANCE_KEY);
  runtime.reportDisplayed(INSTANCE_KEY, 1, first.localFrameId);
  runtime.reportRenderFailure(INSTANCE_KEY, 1, 'old failure', first.localFrameId);

  transport.injectClearFrame(demoClearFrame({ revision: 2 }));
  assert.equal(runtime.getInstance(INSTANCE_KEY), undefined);

  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  const rebuilt = runtime.getInstance(INSTANCE_KEY);
  assert.notEqual(rebuilt.localFrameId, first.localFrameId, 'a rebuilt frame gets a new local generation');
  assert.equal(rebuilt.displayedRevision, 0);
  assert.equal(rebuilt.renderFailure, null);

  runtime.reportDisplayed(INSTANCE_KEY, 1, first.localFrameId);
  assert.equal(runtime.getInstance(INSTANCE_KEY).displayedRevision, 0, 'old generation confirmation is ignored');
  runtime.reportRenderFailure(INSTANCE_KEY, 1, 'stale failure', first.localFrameId);
  assert.equal(runtime.getInstance(INSTANCE_KEY).renderFailure, null, 'old generation failure is ignored');

  runtime.reportDisplayed(INSTANCE_KEY, 1, rebuilt.localFrameId);
  assert.equal(runtime.getInstance(INSTANCE_KEY).displayedRevision, 1);
});

test('dedupe distinguishes submitted payload and current frame', async () => {
  const { transport, runtime } = setup({ autoAccept: false });
  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  const request = (revision, payload) => ({
    instanceKey: INSTANCE_KEY,
    nodeKey: '0.0',
    actionId: 'demo.refresh',
    frameRevision: revision,
    payload,
  });

  const first = runtime.dispatchIntent(request(1, { value: 'a' }));
  const second = runtime.dispatchIntent(request(1, { value: 'ab' }));
  assert.equal(transport.pendingDispatchCount, 2, 'different submitted payload starts a new request');
  transport.resolveNextDispatch({ accepted: true, status: 'handled' });
  transport.resolveNextDispatch({ accepted: true, status: 'handled' });
  assert.equal((await first).status, 'sent');
  assert.equal((await second).status, 'sent');

  transport.injectRenderFrame(demoRenderFrame({ revision: 2 }));
  const nextFrame = runtime.dispatchIntent(request(2, { value: 'a' }));
  assert.equal(transport.pendingDispatchCount, 1, 'same interaction on a new frame starts a new request');
  transport.resolveNextDispatch({ accepted: true, status: 'handled' });
  assert.equal((await nextFrame).status, 'sent');
});

test('releasing an instance consumer drops only instances it no longer covers', async () => {
  const { transport, runtime } = setup();
  const otherKey = 'sess-1|conversation.panel.right|pkg.demo|other';
  const main = runtime.mountSlot('sess-1', DEMO_SLOT, { instanceKey: 'main' });
  const other = runtime.mountSlot('sess-1', DEMO_SLOT, { instanceKey: 'other' });
  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  transport.injectRenderFrame(demoRenderFrame({ revision: 1, target: { instance_key: 'other' } }));

  main.release();
  assert.equal(runtime.getInstance(INSTANCE_KEY), undefined, 'uncovered instance is dropped');
  assert.notEqual(runtime.getInstance(otherKey), undefined, 'instance still covered by another consumer stays');
  assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, true);
  assert.deepEqual(
    await runtime.dispatchIntent({
      instanceKey: INSTANCE_KEY,
      nodeKey: '0.0',
      actionId: 'demo.refresh',
      frameRevision: 1,
    }),
    { status: 'inactive' },
  );

  other.release();
  assert.equal(runtime.getInstance(otherKey), undefined);
  assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, false);
});

test('releasing a slot-wide consumer keeps instances covered by instance consumers', () => {
  const { transport, runtime } = setup();
  const otherKey = 'sess-1|conversation.panel.right|pkg.demo|other';
  const slotWide = runtime.mountSlot('sess-1', DEMO_SLOT);
  const other = runtime.mountSlot('sess-1', DEMO_SLOT, { instanceKey: 'other' });
  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  transport.injectRenderFrame(demoRenderFrame({ revision: 1, target: { instance_key: 'other' } }));

  slotWide.release();
  assert.equal(runtime.getInstance(INSTANCE_KEY), undefined);
  assert.notEqual(runtime.getInstance(otherKey), undefined);
  assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, true);
  other.release();
});

test('reconnect keeps consumer instance identity after content clear', async () => {
  const { transport, runtime } = setup();
  const handle = runtime.mountSlot('sess-1', DEMO_SLOT, { instanceKey: 'main' });
  await handle.notifyReady();
  assert.equal(transport.dispatches.at(-1).target.instanceKey, 'main');
  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  transport.injectClearFrame(demoClearFrame({ revision: 2 }));

  transport.setConnectionState('disconnected');
  transport.setConnectionState('connected');
  await flush();
  const ready = transport.dispatches.filter((request) => request.event === 'ui.ready');
  assert.equal(ready.length, 2);
  assert.equal(ready.at(-1).target.instanceKey, 'main', 'ready identity is kept until the consumer releases');
  handle.release();
});

test('error frames attach to active instances and create error-only instances', () => {
  const { transport, runtime } = setup();
  transport.injectErrorFrame(demoErrorFrame({ revision: 0, code: 'SCHEMA_ERROR', message: 'bad tree' }));
  const errored = runtime.getInstance(INSTANCE_KEY);
  assert.equal(errored.state, 'error');
  assert.equal(errored.frame, null);

  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  assert.equal(runtime.getInstance(INSTANCE_KEY).state, 'active');
  transport.injectErrorFrame(demoErrorFrame({ revision: 0, code: 'DELIVERY_FAILED' }));
  const active = runtime.getInstance(INSTANCE_KEY);
  assert.equal(active.state, 'active');
  assert.equal(active.error.code, 'DELIVERY_FAILED');
  assert.notEqual(active.frame, null);
});

test('clearSession drops one session without touching another', () => {
  const { transport, runtime } = setup();
  transport.injectRenderFrame(demoRenderFrame());
  transport.injectRenderFrame(demoRenderFrame({ target: { session_id: 'sess-2' } }));
  runtime.clearSession('sess-1');
  assert.equal(runtime.getInstance(INSTANCE_KEY), undefined);
  assert.notEqual(runtime.getInstance('sess-2|conversation.panel.right|pkg.demo|main'), undefined);
});

test('start is idempotent and a disposed runtime cannot restart', () => {
  const { transport, runtime } = setup();
  runtime.start();
  assert.equal(transport.dispatches.length, 0);
  runtime.dispose();
  assert.throws(() => runtime.start(), /disposed/);
});

test('unknown slots are rejected at mount time', () => {
  const { runtime } = setup();
  assert.throws(() => runtime.mountSlot('sess-1', 'application.menu'), /not registered/);
  assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT_SINGLE).mounted, false);
});
