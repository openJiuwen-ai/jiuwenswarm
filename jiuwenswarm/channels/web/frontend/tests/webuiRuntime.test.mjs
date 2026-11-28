import test from 'node:test';
import assert from 'node:assert/strict';

import { createWebUIRuntime } from '../node_modules/.cache/webui-runtime/runtime/index.js';
import { createMockTransport } from '../node_modules/.cache/webui-runtime/transport/index.js';
import { DEMO_COMPONENT, DEMO_SLOT, DEMO_SLOT_SINGLE, demoCatalog } from '../node_modules/.cache/webui-runtime/testing/demoCatalog.js';
import {
  demoClearFrame,
  demoErrorFrame,
  demoRenderFrame,
} from '../node_modules/.cache/webui-runtime/testing/fixtures.js';

const INSTANCE_KEY = 'sess-1|conversation.input.above|pkg.demo|main';

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

test('a host restart applies renders whose revisions restarted from 1', async () => {
  const { transport, runtime } = setup();
  const handle = runtime.mountSlot('sess-1', DEMO_SLOT);
  await handle.notifyReady();
  transport.injectRenderFrame(demoRenderFrame({ revision: 7 }));

  transport.setConnectionState('disconnected');
  transport.setConnectionState('connected');
  await flush();
  transport.injectRenderFrame(demoRenderFrame({
    revision: 1,
    root: { component: DEMO_COMPONENT, props: { title: 'restarted' } },
  }));

  const instance = runtime.getInstance(INSTANCE_KEY);
  assert.equal(instance.revision, 1, 'the new host process starts its revisions at 1');
  assert.equal(instance.frame.tree.props.title, 'restarted');
  handle.release();
});

test('a confirmed restore clears stale instances before ready and keeps the mounted consumers', async () => {
  const { transport, runtime } = setup();
  const owner = runtime.mountSlot('sess-1', DEMO_SLOT, { instanceKey: 'main' });
  await owner.notifyReady();
  transport.injectRenderFrame(demoRenderFrame({ revision: 8 }));
  const oldVersion = runtime.getInstanceVersion(INSTANCE_KEY);
  const atReady = [];
  const dispatch = transport.dispatch;
  transport.dispatch = (request) => {
    atReady.push([request.target.instanceKey, runtime.getInstance(INSTANCE_KEY)]);
    transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
    return dispatch(request);
  };
  try {
    const restoredAt = Date.now();
    runtime.confirmSystemPluginRestore('restore-one', restoredAt);
    assert.equal(runtime.getInstance(INSTANCE_KEY).revision, 8);
    transport.setConnectionState('disconnected');
    assert.equal(runtime.getInstance(INSTANCE_KEY).revision, 8);
    transport.setConnectionState('connected');
    assert.deepEqual(atReady, [['main', undefined]]);
    assert.ok(runtime.getInstanceVersion(INSTANCE_KEY) > oldVersion);
    assert.equal(runtime.getInstance(INSTANCE_KEY).revision, 1);
    assert.equal(runtime.getScopeSnapshot('sess-1', DEMO_SLOT).mounted, true);

    runtime.confirmSystemPluginRestore('restore-one', restoredAt);
    transport.setConnectionState('disconnected');
    transport.setConnectionState('connected');
    assert.equal(atReady[1][1].revision, 1, 'ordinary reconnect keeps the new frame');
  } finally {
    owner.release();
    runtime.dispose();
  }
});

test('late confirmation handles an observed disconnect or reconnect without arming the next outage', (t) => {
  let now = 0;
  t.mock.method(Date, 'now', () => now);
  for (const confirmWhileOffline of [true, false]) {
    const { transport, runtime } = setup();
    try {
      transport.injectRenderFrame(demoRenderFrame({ revision: 8 }));
      const restoredAt = Date.now();
      now += 1000;
      transport.setConnectionState('disconnected');
      if (confirmWhileOffline) runtime.confirmSystemPluginRestore('restore-one', restoredAt);
      transport.setConnectionState('connected');
      if (!confirmWhileOffline) {
        assert.equal(runtime.getInstance(INSTANCE_KEY).revision, 8);
        runtime.confirmSystemPluginRestore('restore-one', restoredAt);
      }
      assert.equal(runtime.getInstance(INSTANCE_KEY), undefined);
      transport.injectRenderFrame(demoRenderFrame());
      transport.setConnectionState('disconnected');
      transport.setConnectionState('connected');
      assert.equal(runtime.getInstance(INSTANCE_KEY).revision, 1);
    } finally {
      runtime.dispose();
    }
  }
});

test('only the wait for a restore disconnect expires, not a slow backend restart', (t) => {
  let now = 0;
  t.mock.method(Date, 'now', () => now);
  const { transport, runtime } = setup();
  try {
    transport.injectRenderFrame(demoRenderFrame({ revision: 8 }));
    runtime.confirmSystemPluginRestore('first-restore', now);
    now += 120_000;
    transport.setConnectionState('disconnected');
    transport.setConnectionState('connected');
    assert.equal(runtime.getInstance(INSTANCE_KEY).revision, 8);

    now += 1000;
    runtime.confirmSystemPluginRestore('second-restore', now);
    assert.equal(runtime.getInstance(INSTANCE_KEY).revision, 8, 'a disconnect before this restore does not count');
    transport.setConnectionState('disconnected');
    now += 120_000;
    transport.setConnectionState('connected');
    assert.equal(runtime.getInstance(INSTANCE_KEY), undefined);
  } finally {
    runtime.dispose();
  }
});

function bindDisplayed(runtime, revision) {
  const owner = runtime.mountSlot('sess-1', DEMO_SLOT);
  const snapshot = runtime.getInstance(INSTANCE_KEY);
  runtime.reportDisplayed(INSTANCE_KEY, revision, snapshot.localFrameId);
  return runtime.bindNode(INSTANCE_KEY, snapshot.localFrameId, '0.0', owner.mountLeaseId);
}

test('dispatch requires the current displayed frame and a live connection', async () => {
  const { transport, runtime } = setup();
  transport.injectRenderFrame(demoRenderFrame({ revision: 5 }));
  const stale = await runtime.dispatchIntent({ instanceKey: INSTANCE_KEY, nodeKey: '0.0', frameRevision: 4 });
  assert.deepEqual(stale, { status: 'inactive' });
  const binding = bindDisplayed(runtime, 5);
  const sent = await binding.emit('ui.click', {});
  assert.equal(sent.status, 'sent');
  assert.deepEqual(transport.dispatches.at(-1).payload, {
    action_id: 'demo.refresh', args: { page: 1 }, data: {}, node_key: '0.0', revision: 5,
  });
  transport.setConnectionState('disconnected');
  assert.deepEqual(await binding.emit('ui.click', {}), { status: 'disconnected' });
});

test('repeated in-flight component callbacks are all sent', async () => {
  const { transport, runtime } = setup({ autoAccept: false });
  transport.injectRenderFrame(demoRenderFrame());
  const binding = bindDisplayed(runtime, 1);
  const first = binding.emit('ui.click', {});
  const second = binding.emit('ui.click', {});
  assert.equal(transport.pendingDispatchCount, 2);
  transport.resolveNextDispatch({ accepted: true, status: 'handled' });
  transport.resolveNextDispatch({ accepted: true, status: 'handled' });
  assert.deepEqual((await Promise.all([first, second])).map((result) => result.status), ['sent', 'sent']);
});

test('transport exceptions become a failed interaction result', async () => {
  const { transport, runtime } = setup();
  transport.dispatch = async () => { throw new Error('offline'); };
  transport.injectRenderFrame(demoRenderFrame());
  const result = await bindDisplayed(runtime, 1).emit('ui.click', {});
  assert.deepEqual(result, { status: 'failed', error: 'offline' });
});

test('unsupported schema frames never create or replace a runtime instance', () => {
  const { transport, runtime } = setup();
  transport.injectRenderFrame(demoRenderFrame({ schemaVersion: 2 }));
  assert.equal(runtime.getInstance(INSTANCE_KEY), undefined);
  transport.injectRenderFrame(demoRenderFrame());
  const current = runtime.getInstance(INSTANCE_KEY);
  transport.injectRenderFrame(demoRenderFrame({ schemaVersion: 2, revision: 2 }));
  assert.equal(runtime.getInstance(INSTANCE_KEY), current);
});

test('display feedback and render failures are frame-scoped', () => {
  const { transport, runtime } = setup();
  transport.injectRenderFrame(demoRenderFrame({ revision: 2 }));

  runtime.reportDisplayed(INSTANCE_KEY, 2, runtime.getInstance(INSTANCE_KEY).localFrameId);
  assert.equal(runtime.getInstance(INSTANCE_KEY).displayedRevision, 2);
  runtime.reportDisplayed(INSTANCE_KEY, 1);
  assert.equal(runtime.getInstance(INSTANCE_KEY).displayedRevision, 2, 'stale confirmation ignored');

  runtime.reportRenderFailure(INSTANCE_KEY, 2, 'adapter crash', runtime.getInstance(INSTANCE_KEY).localFrameId);
  assert.deepEqual(runtime.getInstance(INSTANCE_KEY).renderFailure, { revision: 2, message: 'adapter crash' });
  runtime.reportRenderFailure(INSTANCE_KEY, 1, 'old failure');
  assert.equal(runtime.getInstance(INSTANCE_KEY).renderFailure.message, 'adapter crash');

  transport.injectRenderFrame(demoRenderFrame({ revision: 3 }));
  assert.equal(runtime.getInstance(INSTANCE_KEY).renderFailure, null, 'new frame clears old failure');
});

test('error frames attach to active instances and create error-only instances', () => {
  const { transport, runtime } = setup();
  transport.injectErrorFrame(demoErrorFrame({ revision: 0, code: 'SCHEMA_ERROR', message: 'bad tree' }));
  const errored = runtime.getInstance(INSTANCE_KEY);
  assert.equal(errored.state, 'error');
  assert.equal(errored.frame, null);

  transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
  assert.equal(runtime.getInstance(INSTANCE_KEY).state, 'active');
  transport.injectErrorFrame(demoErrorFrame({ revision: 1, code: 'DELIVERY_FAILED' }));
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
  assert.notEqual(runtime.getInstance('sess-2|conversation.input.above|pkg.demo|main'), undefined);
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

test('releasing a slot-wide consumer keeps instances covered by instance consumers', () => {
  const { transport, runtime } = setup();
  const otherKey = 'sess-1|conversation.input.above|pkg.demo|other';
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


test('package change notification refreshes matching mounted owners once per target', async () => {
  const { transport, runtime } = setup();
  const first = runtime.mountSlot('sess-1', DEMO_SLOT);
  const duplicate = runtime.mountSlot('sess-1', DEMO_SLOT);
  const other = runtime.mountSlot('sess-2', DEMO_SLOT);
  await flush();
  const before = transport.dispatches.length;
  transport.injectPackagesChanged(['pkg.new', 'pkg.new'], 'sess-1');
  await flush();
  const added = transport.dispatches.slice(before);
  assert.equal(added.length, 1);
  assert.equal(added[0].event, 'ui.ready');
  assert.equal(added[0].target.packageId, 'pkg.new');
  assert.equal(added[0].target.sessionId, 'sess-1');
  first.release();
  duplicate.release();
  const afterRelease = transport.dispatches.length;
  transport.injectPackagesChanged(['pkg.new'], 'sess-1');
  await flush();
  assert.equal(transport.dispatches.length, afterRelease);
  other.release();
  runtime.dispose();
});

test('package change notification preserves the host notifyReady opt-out', async () => {
  const { transport, runtime } = setup();
  const owner = runtime.mountSlot('sess-1', DEMO_SLOT, { notifyReady: false });
  await flush();
  assert.equal(transport.dispatches.length, 0);
  transport.injectPackagesChanged(['pkg.new'], 'sess-1');
  await flush();
  assert.equal(transport.dispatches.length, 0);
  owner.release();
  runtime.dispose();
});

const colorsSnapshot = (revision, accent, packageId = 'pkg.theme') => ({
  revision, parts: { colors: { package_id: packageId, value: { accent } } },
});

test('appearance snapshots apply in revision order and ignore malformed payloads', () => {
  const { transport, runtime } = setup();
  let notifications = 0;
  runtime.subscribeAppearance(() => { notifications += 1; });
  transport.injectAppearanceFrame(colorsSnapshot(2, '#b45309'));
  assert.deepEqual(runtime.getAppearance(), {
    revision: 2, parts: { colors: { packageId: 'pkg.theme', value: { accent: '#b45309' } } },
  });
  for (const payload of [
    colorsSnapshot(1, '#2f6fae'),
    colorsSnapshot(2, '#2f6fae'),
    colorsSnapshot(3, 'red'),
    { revision: 3, parts: { colors: { package_id: 'pkg.theme', value: {} } } },
    { revision: 3, parts: { colors: { package_id: '', value: { accent: '#2f6fae' } } } },
    { revision: 3, parts: { colors: { package_id: 'pkg.theme', value: { accent: '#2f6fae' }, extra: true } } },
    { ...colorsSnapshot(3, '#2f6fae'), extra: true },
    // The former single-provider frame is not a snapshot.
    { package_id: 'pkg.theme', revision: 3, appearance: { accent: '#2f6fae' } },
    { revision: 3, parts: null },
    { revision: 0, parts: {} },
    { revision: 3.5, parts: {} },
    'webui.appearance',
  ]) transport.injectAppearanceFrame(payload);
  assert.equal(runtime.getAppearance().parts.colors.value.accent, '#b45309');
  assert.equal(notifications, 1);
  transport.injectAppearanceFrame({ revision: 3, parts: {
    colors: { package_id: 'pkg.theme', value: { accent: '#2f6fae' } },
    identity: { package_id: 'pkg.logo', value: { name: ' Acme ', logo: '/extension-assets/pkg.logo/g1/logo.svg' } },
  } });
  assert.deepEqual(runtime.getAppearance().parts.identity, {
    packageId: 'pkg.logo',
    value: { name: 'Acme', logo: '/extension-assets/pkg.logo/g1/logo.svg', favicon: '/extension-assets/pkg.logo/g1/logo.svg' },
  }, 'part values are read in their normalized form');
  transport.injectAppearanceFrame({ revision: 4, parts: {} });
  assert.deepEqual(runtime.getAppearance(), { revision: 4, parts: {} }, 'an empty snapshot means host defaults');
  assert.equal(notifications, 3);
});

test('the host appearance snapshot applies its colors part', () => {
  const { transport, runtime } = setup();
  const colors = (accent) => ({ package_id: 'pkg.theme', value: { accent } });
  transport.injectAppearanceFrame({ revision: 1, parts: { colors: colors('#2f6fae') } });
  assert.deepEqual(runtime.getAppearance(), {
    revision: 1,
    parts: { colors: { packageId: 'pkg.theme', value: { accent: '#2f6fae' } } },
  });

  // A part this client does not render yet must not hide the colors sent with it.
  transport.injectAppearanceFrame({
    revision: 2,
    parts: { border: { package_id: 'pkg.brand', value: {} }, colors: colors('#b45309') },
  });
  assert.deepEqual(runtime.getAppearance(), {
    revision: 2,
    parts: { colors: { packageId: 'pkg.theme', value: { accent: '#b45309' } } },
  });

  // Malformed color values are ignored as a whole and keep the accepted frame.
  for (const payload of [
    { revision: 3, parts: { colors: { package_id: 'pkg.theme', value: { accent: 'red' } } } },
    { revision: 3, parts: { colors: { package_id: '', value: { accent: '#2f6fae' } } } },
    { revision: 3, parts: 'nope' },
  ]) transport.injectAppearanceFrame(payload);
  assert.equal(runtime.getAppearance().revision, 2);

  // A snapshot without colors means the host defaults apply.
  transport.injectAppearanceFrame({ revision: 3, parts: {} });
  assert.deepEqual(runtime.getAppearance(), { revision: 3, parts: {} });
  runtime.dispose();
});

test('held snapshots wait for every hold and then show only the latest one', () => {
  const { transport, runtime } = setup();
  const seen = [];
  runtime.subscribeAppearance(() => seen.push(runtime.getAppearance()?.revision ?? null));
  const first = runtime.holdAppearance();
  const second = runtime.holdAppearance();
  transport.injectAppearanceFrame(colorsSnapshot(1, '#b45309'));
  transport.injectAppearanceFrame(colorsSnapshot(2, '#2f6fae'));
  assert.equal(runtime.getAppearance(), null, 'nothing shows while held');
  first();
  first();
  assert.equal(runtime.getAppearance(), null, 'a release counts once');
  second();
  assert.equal(runtime.getAppearance().revision, 2);
  assert.deepEqual(seen, [2], 'the intermediate snapshot never showed');
  transport.injectAppearanceFrame(colorsSnapshot(3, '#0d8f72'));
  assert.equal(runtime.getAppearance().revision, 3, 'without holds snapshots show at once');
  runtime.dispose();
});

test('a ready start hook runs before the notification and settling follows it', async () => {
  const { transport, runtime } = setup({ autoAccept: false });
  const events = [];
  const handle = runtime.mountSlot('sess-1', DEMO_SLOT, {
    onReadyStart: () => events.push(`start:${transport.dispatches.length}`),
    onReadySettled: (receipt) => events.push(`settled:${receipt?.status ?? null}`),
  });
  const ready = handle.notifyReady();
  assert.deepEqual(events, ['start:0']);
  transport.resolveNextDispatch({ accepted: true, status: 'ready' });
  await ready;
  assert.deepEqual(events, ['start:0', 'settled:ready']);
  handle.release();
  runtime.dispose();
});

test('appearance survives a reconnect only when a provider re-applies it', async () => {
  const { transport, runtime } = setup();
  const handle = runtime.mountSlot('sess-1', DEMO_SLOT);
  await handle.notifyReady();
  transport.injectAppearanceFrame({ revision: 5, parts: { colors: { package_id: 'pkg.theme', value: { surface: 'translucent' } } } });

  transport.setConnectionState('disconnected');
  assert.equal(runtime.getAppearance().revision, 5, 'kept while disconnected');
  transport.injectAppearanceFrame({ revision: 9, parts: {} });
  assert.equal(runtime.getAppearance().revision, 5, 'frames are not accepted while disconnected');
  transport.setConnectionState('connected');
  // Revisions restart with the new connection.
  transport.injectAppearanceFrame(colorsSnapshot(1, '#b45309'));
  await flush();
  assert.deepEqual(runtime.getAppearance().parts.colors.value, { accent: '#b45309' });

  transport.setConnectionState('disconnected');
  transport.setConnectionState('connected');
  await flush();
  assert.equal(runtime.getAppearance(), null, 'no provider re-applied after the reconnect readies settled');
  handle.release();
  runtime.dispose();
});
