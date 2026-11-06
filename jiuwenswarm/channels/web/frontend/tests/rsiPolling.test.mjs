import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';

const mocks = {
  './rsiApi': `
    export const rsiTaskGet = id => globalThis.rsiPollingProbe('task', id);
    export const rsiReportGet = id => globalThis.rsiPollingProbe('report', id);
    export const rsiUsageGet = id => globalThis.rsiPollingProbe('usage', id);
    export const rsiTreeGet = id => globalThis.rsiPollingProbe('tree', id);
    export const rsiTaskList = async () => [];
  `,
  '../rsiStore': 'export const useRsiStore = selector => selector(globalThis.rsiPollingState);',
  'react-i18next': 'export const useTranslation = () => ({ t: key => key });',
};
for (const name of [
  'RsiDetailHeader',
  'RsiResultSummary',
  'RsiCanvasArea',
  'ConfigInfoDialog',
  'RsiArtifactDetailDialog',
]) {
  mocks[`./${name}`] = `export const ${name} = () => null;`;
}
await build({
  entryPoints: ['src/features/rsi/rsiStore.ts', 'src/features/rsi/components/RsiDetail.tsx'],
  outdir: 'node_modules/.cache/rsi-polling',
  outbase: 'src/features/rsi',
  outExtension: { '.js': '.mjs' },
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  jsx: 'automatic',
  plugins: [
    {
      name: 'test-boundaries',
      setup(builder) {
        builder.onResolve({ filter: /.*/ }, (args) =>
          args.path in mocks ? { path: args.path, namespace: 'mock' } : undefined,
        );
        builder.onLoad({ filter: /.*/, namespace: 'mock' }, (args) => ({ contents: mocks[args.path] }));
      },
    },
  ],
});
const { useRsiStore } = await import('../node_modules/.cache/rsi-polling/rsiStore.mjs');
const { RsiDetail } = await import('../node_modules/.cache/rsi-polling/components/RsiDetail.mjs');
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};
const tick = () => new Promise((resolve) => setImmediate(resolve));

test('overlapping poll and push refreshes send only one group and share completion', async () => {
  const held = deferred();
  const calls = [];
  globalThis.rsiPollingProbe = async (method, id) => {
    calls.push([method, id]);
    await held.promise;
    return method === 'tree' ? { nodes: [] } : { task_id: id, status: 'RUNNING' };
  };
  const refresh = useRsiStore.getState().refreshDetail;
  const first = refresh('one');
  const repeated = Array.from({ length: 20 }, () => refresh('one'));
  assert.ok(repeated.every((request) => request === first));
  await tick();
  assert.equal(calls.length, 4);
  held.resolve();
  await Promise.all([first, ...repeated]);
  await refresh('one');
  assert.equal(calls.length, 8);
  assert.equal(useRsiStore.getState().detail.one.task.status, 'RUNNING');
});

test('a pending task does not block another task; failures release the request', async () => {
  const held = deferred();
  const calls = [];
  globalThis.rsiPollingProbe = async (method, id) => {
    calls.push([method, id]);
    if (id === 'slow') await held.promise;
    if (id === 'error') throw Error('timeout');
    return method === 'tree' ? { nodes: [] } : { task_id: id };
  };
  const refresh = useRsiStore.getState().refreshDetail;
  const slow = refresh('slow');
  await refresh('fast');
  assert.equal(useRsiStore.getState().detail.fast.task.task_id, 'fast');
  held.resolve();
  await slow;
  const original = console.error;
  console.error = () => {};
  try {
    await refresh('error');
    await refresh('error');
    assert.equal(calls.filter(([, id]) => id === 'error').length, 8);
  } finally {
    console.error = original;
  }
});

test('poll timer is scheduled after completion and cannot restart after unmount', async () => {
  const dom = new JSDOM('<div id="root"></div>');
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const timers = new Map();
  let timerId = 0;
  dom.window.setTimeout = (callback) => {
    timers.set(++timerId, callback);
    return timerId;
  };
  dom.window.clearTimeout = (id) => timers.delete(id);
  const pending = [];
  globalThis.rsiPollingState = {
    selectedTaskId: 'one',
    detail: { one: { task: { status: 'RUNNING' } } },
    detailLoading: false,
    list: [],
    refreshDetail: () => {
      const request = deferred();
      pending.push(request);
      return request.promise;
    },
  };
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(React.createElement(RsiDetail)));
    assert.equal(timers.size, 1);
    const [id, poll] = timers.entries().next().value;
    timers.delete(id);
    const polling = poll();
    assert.equal(timers.size, 0);
    pending.at(-1).resolve();
    await polling;
    assert.equal(timers.size, 1);
    const [nextId, nextPoll] = timers.entries().next().value;
    timers.delete(nextId);
    const last = nextPoll();
    await act(async () => root.unmount());
    pending.forEach((request) => request.resolve());
    await last;
    assert.equal(timers.size, 0);
  } finally {
    pending.forEach((request) => request.resolve());
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
    delete globalThis.IS_REACT_ACT_ENVIRONMENT;
  }
});
