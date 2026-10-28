import assert from 'node:assert/strict';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

import { groupByWorkflowRun, shortRunId } from '../node_modules/.cache/run-grouping/runGrouping.js';
import { RunGroupHeader } from '../node_modules/.cache/run-grouping/RunGroupHeader.js';

function installGlobals(values) {
  const previousDescriptors = new Map();
  for (const [name, value] of Object.entries(values)) {
    previousDescriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }

  return () => {
    for (const [name, descriptor] of previousDescriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  };
}

function createI18n() {
  const instance = i18next.createInstance();
  void instance.init({
    lng: 'en',
    fallbackLng: 'en',
    initImmediate: false,
    showSupportNotice: false,
    resources: {
      en: {
        translation: {
          swarmflow: {
            statusPlanned: 'Planned',
            statusPending: 'Pending',
            statusRunning: 'Running',
            statusPaused: 'Paused',
            statusCompleted: 'Completed',
            statusFailed: 'Failed',
            statusStopped: 'Stopped',
            statusWaitingForHuman: 'Waiting for human input',
            runIdTooltip: 'Run ID: {{id}}',
          },
        },
      },
    },
  });
  return instance;
}

const RUNS = [
  { id: 'wf-aaa-bbb-ccc', name: 'Market research', status: 'running' },
  { id: 'wf-ddd-eee-fff', name: 'Code review', status: 'completed' },
];

test('groups by workflow_run_id and keeps non-workflow items ungrouped', () => {
  const items = [
    { task_id: 't1', workflow_run_id: 'wf-aaa-bbb-ccc' },
    { task_id: 't2' },
    { task_id: 't3', workflow_run_id: 'wf-ddd-eee-fff' },
    { task_id: 't4', workflow_run_id: 'wf-aaa-bbb-ccc' },
  ];
  const { ungrouped, groups } = groupByWorkflowRun(items, RUNS);

  assert.deepEqual(ungrouped.map((item) => item.task_id), ['t2']);
  assert.deepEqual(groups.map((group) => group.runId), ['wf-aaa-bbb-ccc', 'wf-ddd-eee-fff']);
  assert.deepEqual(groups[0].items.map((item) => item.task_id), ['t1', 't4']);
  assert.equal(groups[0].name, 'Market research');
  assert.equal(groups[0].status, 'running');
  assert.equal(groups[0].shortId, 'wf-aaa-b');
  assert.deepEqual(groups[1].items.map((item) => item.task_id), ['t3']);
});

test('items without workflow_run_id pass through untouched', () => {
  const items = [{ a: 1 }, { b: 2, workflow_run_id: undefined }];
  const { ungrouped, groups } = groupByWorkflowRun(items, RUNS);
  assert.equal(ungrouped.length, 2);
  assert.equal(groups.length, 0);
});

test('group order is stable by first appearance of the run in the items', () => {
  const items = [
    { id: 'x', workflow_run_id: 'run-b' },
    { id: 'y', workflow_run_id: 'run-a' },
    { id: 'z', workflow_run_id: 'run-b' },
  ];
  const { groups } = groupByWorkflowRun(items, []);
  assert.deepEqual(groups.map((group) => group.runId), ['run-b', 'run-a']);
  assert.deepEqual(groups[0].items.map((item) => item.id), ['x', 'z']);
});

test('run missing from the summary source falls back to run id name and empty status', () => {
  const { groups } = groupByWorkflowRun([{ workflow_run_id: 'run-unknown-12345' }], RUNS);
  assert.equal(groups[0].name, 'run-unknown-12345');
  assert.equal(groups[0].status, '');
  assert.equal(groups[0].shortId, 'run-unkn');
});

test('shortRunId truncates to 8 chars and passes shorter ids through', () => {
  assert.equal(shortRunId('abcdefghijklmn'), 'abcdefgh');
  assert.equal(shortRunId('abcdefgh'), 'abcdefgh');
  assert.equal(shortRunId('abc'), 'abc');
});

test('RunGroupHeader renders name, short id, and status badge', async () => {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>');
  const restore = installGlobals({
    IS_REACT_ACT_ENVIRONMENT: true,
    Node: dom.window.Node,
    HTMLElement: dom.window.HTMLElement,
    document: dom.window.document,
    navigator: dom.window.navigator,
    window: dom.window,
    requestAnimationFrame: (callback) => setTimeout(callback, 0),
    cancelAnimationFrame: (id) => clearTimeout(id),
  });
  const container = dom.window.document.querySelector('#root');
  const i18n = createI18n();
  let root;
  try {
    root = createRoot(container);
    await act(async () => {
      root.render(
        createElement(
          I18nextProvider,
          { i18n },
          createElement(RunGroupHeader, {
            name: 'Market research',
            shortId: 'wf-aaa-bb',
            status: 'running',
            runId: 'wf-aaa-bbb-ccc',
          }),
        ),
      );
    });

    assert.equal(
      container.querySelector('[data-testid="team-area-run-group-header-name"]')?.textContent,
      'Market research',
    );
    const shortId = container.querySelector('[data-testid="team-area-run-group-header-short-id"]');
    assert.equal(shortId?.textContent, '#wf-aaa-bb');
    assert.equal(shortId?.getAttribute('title'), 'Run ID: wf-aaa-bbb-ccc');
    const statusBadge = container.querySelector('[data-testid="team-area-run-group-header-status"]');
    assert.equal(statusBadge?.getAttribute('data-variant'), 'running');
    assert.match(statusBadge?.textContent ?? '', /Running/);
  } finally {
    if (root) await act(async () => root.unmount());
    restore();
    dom.window.close();
  }
});
