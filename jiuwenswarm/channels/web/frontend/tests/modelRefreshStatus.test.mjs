import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { useModelRefreshStatus } from '../node_modules/.cache/model-refresh/useModelRefreshStatus.js';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function mount(t, request, connected = true) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const dom = new JSDOM('<div id="root"></div>');
  const previous = new Map();
  for (const [key, value] of Object.entries({
    window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  let current;
  function Probe({ connected }) {
    current = useModelRefreshStatus(connected, request);
    return createElement('output', null, JSON.stringify(current.refreshStatus));
  }
  const root = createRoot(dom.window.document.getElementById('root'));
  const render = async (connected) => act(async () => root.render(createElement(Probe, { connected })));
  await render(connected);
  t.after(async () => {
    await act(async () => root.unmount());
    dom.window.close();
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return {
    status: () => current.refreshStatus,
    check: async () => act(async () => current.checkRefreshStatus()),
    tick: async (ms) => act(async () => t.mock.timers.tick(ms)),
    render,
    unmount: async () => act(async () => root.unmount()),
  };
}

// Removing the pending branch would stop observation before the background refresh completes.
test('mount observes pending, polls to applied and stops polling', async (t) => {
  const replies = [{ status: 'pending', generation: 2 }, { status: 'applied', generation: 2 }];
  const calls = [];
  const view = await mount(t, async (method, params, options) => {
    calls.push({ method, params, options });
    return replies.shift();
  });
  assert.deepEqual(view.status(), { status: 'pending', generation: 2 });
  assert.equal(calls[0].method, 'models.refresh_status');
  assert.equal(calls[0].options.timeoutMs, 5000);
  assert.ok(calls[0].options.signal instanceof AbortSignal);
  await view.tick(2000);
  assert.deepEqual(view.status(), { status: 'applied', generation: 2 });
  await view.tick(20000);
  assert.equal(calls.length, 2);
});

// Treating failed as applied or clearing it on a timer would conceal background failure.
test('background failure remains visible and does not poll a terminal status', async (t) => {
  let calls = 0;
  const view = await mount(t, async () => {
    calls += 1;
    return { status: 'failed', generation: 3, detail: 'Model refresh failed.' };
  });
  assert.deepEqual(view.status(), { status: 'failed', generation: 3, detail: 'Model refresh failed.' });
  await view.tick(20000);
  assert.equal(view.status().status, 'failed');
  assert.equal(calls, 1);
});

test('the initial applied generation zero is accepted', async (t) => {
  const view = await mount(t, async () => ({ status: 'applied', generation: 0 }));
  assert.deepEqual(view.status(), { status: 'applied', generation: 0 });
});

test('RPC rejection reports unavailable without exposing arbitrary error text and can retry', async (t) => {
  let fail = true;
  const view = await mount(t, async () => {
    if (fail) throw new Error('private transport detail');
    return { status: 'pending', generation: 4 };
  });
  assert.deepEqual(view.status(), { status: 'unavailable' });
  fail = false;
  await view.check();
  assert.deepEqual(view.status(), { status: 'pending', generation: 4 });
});

test('invalid statuses and generations never become an applied result', async (t) => {
  let payload;
  const view = await mount(t, async () => payload, false);
  for (payload of [null, {}, { status: 'done', generation: 1 }, { status: 'applied', generation: -1 },
    { status: 'applied', generation: 1.5 }, { status: 'applied', generation: '1' },
    { status: ['applied'], generation: 1 },
    { status: 'failed', generation: 1, detail: {} }]) {
    await view.render(true);
    await view.check();
    assert.deepEqual(view.status(), { status: 'unavailable' });
  }
});

test('a stalled RPC times out locally, aborts transport and ignores its late success', async (t) => {
  const reply = deferred();
  let signal;
  const view = await mount(t, (_method, _params, options) => {
    signal = options.signal;
    return reply.promise;
  });
  await view.tick(5000);
  assert.deepEqual(view.status(), { status: 'unavailable' });
  assert.equal(signal.aborted, true);
  await act(async () => reply.resolve({ status: 'applied', generation: 1 }));
  assert.deepEqual(view.status(), { status: 'unavailable' });
});

test('post-save check supersedes a pending request and ignores its late failure', async (t) => {
  const first = deferred();
  let calls = 0;
  let firstSignal;
  const view = await mount(t, (_method, _params, options) => {
    calls += 1;
    if (calls === 1) { firstSignal = options.signal; return first.promise; }
    return Promise.resolve({ status: 'pending', generation: 5 });
  });
  await view.check();
  assert.equal(firstSignal.aborted, true);
  assert.deepEqual(view.status(), { status: 'pending', generation: 5 });
  await act(async () => first.resolve({ status: 'failed', generation: 4 }));
  assert.deepEqual(view.status(), { status: 'pending', generation: 5 });
});

test('disconnect cancels the old request and reconnect reads the new server generation', async (t) => {
  const first = deferred();
  let calls = 0;
  let firstSignal;
  const view = await mount(t, (_method, _params, options) => {
    calls += 1;
    if (calls === 1) { firstSignal = options.signal; return first.promise; }
    return Promise.resolve({ status: 'applied', generation: 0 });
  });
  await view.render(false);
  assert.equal(firstSignal.aborted, true);
  await view.check();
  assert.equal(calls, 1);
  await view.render(true);
  await act(async () => first.resolve({ status: 'failed', generation: 8 }));
  assert.deepEqual(view.status(), { status: 'applied', generation: 0 });
});

test('unmount cancels in-flight RPC and scheduled polls', async (t) => {
  let calls = 0;
  const reply = deferred();
  let signal;
  const view = await mount(t, (_method, _params, options) => {
    calls += 1;
    signal = options.signal;
    return calls === 1 ? Promise.resolve({ status: 'pending', generation: 1 }) : reply.promise;
  });
  await view.tick(2000);
  await view.unmount();
  assert.equal(signal.aborted, true);
  await act(async () => reply.resolve({ status: 'pending', generation: 1 }));
  await view.tick(20000);
  assert.equal(calls, 2);
});
