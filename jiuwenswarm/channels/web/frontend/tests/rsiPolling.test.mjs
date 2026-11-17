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

for (const status of ['FAILED', 'COMPLETED', 'TERMINATED', 'PAUSED']) {
  test(`polling ${status} reconciles the sidebar and clears stale live progress`, async () => {
    useRsiStore.setState({
      list: [{ task_id: 'terminal', status: 'RUNNING', running: true }],
      detail: { terminal: { task: { status: 'RUNNING' }, liveProgress: { iteration: 0 }, pendingTreeNodes: [] } },
    });
    globalThis.rsiPollingProbe = async (method) => {
      if (method === 'task') return { task_id: 'terminal', status, progress: { iteration: 1 } };
      if (method === 'report') return { metrics: { iterations: 1, pruned_count: 1 } };
      return method === 'tree' ? { nodes: [] } : {};
    };
    await useRsiStore.getState().refreshDetail('terminal');
    const state = useRsiStore.getState();
    assert.equal(state.list[0].status, status);
    assert.equal(state.list[0].running, false);
    assert.equal(state.detail.terminal.liveProgress, null);
    assert.equal(state.detail.terminal.report.metrics.iterations, 1);
  });
}

test('late progress cannot replace a terminal task snapshot', () => {
  useRsiStore.setState({
    list: [{ task_id: 'done', status: 'FAILED', running: false }],
    detail: { done: { task: { status: 'FAILED' }, liveProgress: null } },
  });
  useRsiStore.getState().applyProgress({ task_id: 'done', iteration: 0, total_iterations: 1 });
  assert.equal(useRsiStore.getState().detail.done.liveProgress, null);
});

test('an in-flight running snapshot cannot undo a paused status without a later push', async () => {
  useRsiStore.setState({
    selectedTaskId: null,
    list: [{ task_id: 'paused', status: 'PAUSED', running: false }],
    detail: { paused: { task: { task_id: 'paused', status: 'PAUSED' }, pendingTreeNodes: [] } },
  });
  const held = deferred();
  globalThis.rsiPollingProbe = async (method) => {
    await held.promise;
    return method === 'tree' ? { nodes: [] } : { task_id: 'paused', status: 'RUNNING' };
  };
  const pending = useRsiStore.getState().refreshDetail('paused');
  await tick();
  held.resolve();
  await pending;
  const state = useRsiStore.getState();
  assert.equal(state.detail.paused.task.status, 'PAUSED');
  assert.equal(state.list[0].running, false);
});

test('an in-flight running snapshot cannot undo a terminal status push', async () => {
  useRsiStore.setState({
    selectedTaskId: null,
    list: [{ task_id: 'race', status: 'RUNNING', running: true }],
    detail: { race: { task: { task_id: 'race', status: 'RUNNING' }, pendingTreeNodes: [] } },
  });
  const held = deferred();
  globalThis.rsiPollingProbe = async (method) => {
    await held.promise;
    return method === 'tree' ? { nodes: [] } : { task_id: 'race', status: 'RUNNING' };
  };
  const pending = useRsiStore.getState().refreshDetail('race');
  await tick();
  useRsiStore.getState().applyStatusChanged({ task_id: 'race', status: 'FAILED', failure_reason: 'HTTP 429' });
  held.resolve();
  await pending;
  const state = useRsiStore.getState();
  assert.equal(state.detail.race.task.status, 'FAILED');
  assert.equal(state.detail.race.task.failure_reason, 'HTTP 429');
  assert.equal(state.list[0].running, false);
});

test('a terminal push during polling schedules a fresh final report', async () => {
  useRsiStore.setState({
    selectedTaskId: 'final',
    list: [{ task_id: 'final', status: 'RUNNING', running: true }],
    detail: { final: { task: { task_id: 'final', status: 'RUNNING' }, pendingTreeNodes: [] } },
  });
  const held = deferred();
  let taskReads = 0;
  let reportReads = 0;
  globalThis.rsiPollingProbe = async (method) => {
    const first = method === 'task' ? ++taskReads === 1 : method === 'report' ? ++reportReads === 1 : false;
    if (first) await held.promise;
    if (method === 'task') return { task_id: 'final', status: first ? 'RUNNING' : 'FAILED' };
    if (method === 'report') return { metrics: { iterations: first ? 0 : 1 } };
    return method === 'tree' ? { nodes: [] } : {};
  };
  const pending = useRsiStore.getState().refreshDetail('final');
  await tick();
  useRsiStore.getState().applyStatusChanged({ task_id: 'final', status: 'FAILED', failure_reason: 'HTTP 429' });
  held.resolve();
  await pending;
  await tick();
  assert.equal(useRsiStore.getState().detail.final.report.metrics.iterations, 1);
  assert.equal(taskReads, 2);
});

for (const [status, initialTask] of [['FAILED', null], ['PAUSED', { status: 'RUNNING' }]]) {
  test(`a ${status} update survives an older poll even without an existing task snapshot`, async () => {
    useRsiStore.setState({
      selectedTaskId: null,
      list: [{ task_id: 'status-race', status: 'RUNNING', running: true }],
      detail: { 'status-race': { task: initialTask, pendingTreeNodes: [] } },
    });
    const held = deferred();
    globalThis.rsiPollingProbe = async (method) => {
      await held.promise;
      return method === 'tree' ? { nodes: [] } : { task_id: 'status-race', status: 'RUNNING' };
    };
    const pending = useRsiStore.getState().refreshDetail('status-race');
    await tick();
    useRsiStore.getState().applyStatusChanged({ task_id: 'status-race', status, failure_reason: 'HTTP 429' });
    held.resolve();
    await pending;
    const state = useRsiStore.getState();
    assert.equal(state.detail['status-race'].task.status, status);
    assert.equal(state.list[0].running, false);
    if (status === 'FAILED') assert.equal(state.detail['status-race'].task.failure_reason, 'HTTP 429');
  });
}

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
