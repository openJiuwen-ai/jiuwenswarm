import assert from 'node:assert/strict';
import { test } from 'node:test';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

const outfile = resolve('node_modules/.cache/application-task-controls/test.mjs');
await build({
  stdin: {
    contents: `export * from './src/applicationPlugins/ApplicationTaskControls'; export * from './src/applicationPlugins/taskProgressStore';`,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  outfile,
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  jsx: 'automatic',
  plugins: [
    {
      name: 'translations',
      setup(builder) {
        builder.onResolve({ filter: /^react-i18next$/ }, () => ({ path: 'i18n', namespace: 'test' }));
        builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({
          contents: 'export const useTranslation = () => ({t: key => key});',
        }));
      },
    },
  ],
});
const {
  ApplicationTaskControls,
  useApplicationTaskStore,
  registerApplicationTaskController,
  applicationTasksToTeamTasks,
} = await import(pathToFileURL(outfile));

test('progress controls stop, prioritize, preempt and drag the exact task without optimistic state changes', async () => {
  const dom = new JSDOM('<div id="root"></div>');
  const previousWindow = globalThis.window;
  const previousDocument = globalThis.document;
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const jobs = ['a', 'b', 'c'].map((id, i) => ({
    id,
    pluginId: 'video-duplex',
    title: id,
    status: i ? 'queued' : 'running',
    sequence: 1,
    createdAt: i,
    detail: '',
    searchSessionId: 'scope',
    queueVersion: 5,
    queuePosition: i,
  }));
  useApplicationTaskStore.setState({ sessions: { conversation: jobs } });
  const calls = [];
  const off = registerApplicationTaskController('video-duplex', async (...args) => {
    calls.push(args);
  });
  const root = createRoot(document.getElementById('root'));
  const label = (key) => `[aria-label="chat.applicationTasks.controls.${key}"]`;
  try {
    await act(async () =>
      root.render(
        createElement(
          'div',
          {},
          ...jobs.map((job) =>
            createElement(
              'div',
              { key: job.id, 'data-job': job.id },
              createElement(ApplicationTaskControls, { taskId: `application:video-duplex:${job.id}` }, job.title),
            ),
          ),
        ),
      ),
    );
    const row = (id) => document.querySelector(`[data-job="${id}"]`);
    await act(async () => row('a').querySelector(label('stop')).click());
    assert.equal(calls.at(-1)[0].id, 'a');
    assert.equal(calls.at(-1)[1], 'cancel');
    assert.equal(useApplicationTaskStore.getState().sessions.conversation[0].status, 'running');
    await act(async () => row('c').querySelector(label('next')).click());
    assert.equal(calls.at(-1)[0].id, 'c');
    assert.equal(calls.at(-1)[1], 'next');
    await act(async () => row('c').querySelector(label('more')).click());
    await act(async () =>
      [...row('c').querySelectorAll('button')].find((button) => button.textContent.endsWith('.preempt')).click(),
    );
    assert.equal(calls.at(-1)[1], 'preempt');
    const drop = new window.Event('drop', { bubbles: true, cancelable: true });
    Object.defineProperty(drop, 'dataTransfer', { value: { getData: () => 'c' } });
    await act(async () => row('b').querySelector('[draggable]').parentElement.dispatchEvent(drop));
    assert.equal(calls.at(-1)[0].id, 'c');
    assert.equal(calls.at(-1)[1], 'before');
    assert.equal(calls.at(-1)[2], 'b');
    await act(async () =>
      useApplicationTaskStore.getState().upsert('conversation', { ...jobs[0], sequence: 2, status: 'cancelling' }),
    );
    assert.equal(row('a').querySelector(label('stop')), null);
    const ordered = applicationTasksToTeamTasks(
      [jobs[0], { ...jobs[1], queuePosition: 2 }, { ...jobs[2], queuePosition: 1 }],
      {},
    );
    assert.deepEqual(
      ordered.map((task) => task.task_id),
      ['application:video-duplex:a', 'application:video-duplex:c', 'application:video-duplex:b'],
    );
  } finally {
    await act(async () => root.unmount());
    off();
    dom.window.close();
    globalThis.window = previousWindow;
    globalThis.document = previousDocument;
    delete globalThis.IS_REACT_ACT_ENVIRONMENT;
  }
});
