import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';

const mocks = {
  '../rsiStore': 'export const useRsiStore = selector => selector(globalThis.rsiInstallProbe.store);',
  '../../../stores/pluginPackageStore': 'export const usePluginPackageStore = { getState: () => globalThis.rsiInstallProbe.plugins };',
  'react-i18next': 'export const useTranslation = () => ({ t: key => key });',
  '../rsiApi': `
    export const rsiHarnessInstall = id => globalThis.rsiInstallProbe.install(id);
    export const rsiTaskDelete = () => {}, rsiTrainingPause = () => {}, rsiTrainingResume = () => {},
      rsiTrainingTerminate = () => {}, rsiArtifactDownload = () => {}, rsiArtifactDownloadUrl = () => {};
  `,
};
await build({
  entryPoints: ['src/features/rsi/components/RsiDetailHeader.tsx'],
  outfile: 'node_modules/.cache/rsi-plugin-install/header.mjs',
  bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic',
  loader: { '.svg': 'dataurl' },
  plugins: [{ name: 'test-boundaries', setup(builder) {
    builder.onResolve({ filter: /.*/ }, args => args.path in mocks ? { path: args.path, namespace: 'mock' } : undefined);
    builder.onLoad({ filter: /.*/, namespace: 'mock' }, args => ({ contents: mocks[args.path] }));
  } }],
});
const { RsiDetailHeader } = await import('../node_modules/.cache/rsi-plugin-install/header.mjs');

test('RSI install refreshes the shared extension list only after backend success', async () => {
  const dom = new JSDOM('<div id="root"></div>');
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const calls = [];
  globalThis.rsiInstallProbe = {
    store: { installedTaskIds: {}, markTaskInstalled: id => calls.push(['installed', id]) },
    install: async id => { calls.push(['install', id]); },
    plugins: { loadList: async (...args) => calls.push(['refresh', ...args]) },
  };
  const root = createRoot(document.getElementById('root'));
  const props = {
    task: { task_id: 'task-1', name: 'Training', status: 'COMPLETED', scenario: 'HARNESS', config: {} },
    tree: { nodes: [{ node_id: 'ROOT', type: 'ROOT' }, { node_id: 'e1', type: 'ADOPTED' }] },
    report: null, liveCost: null, createdAt: null,
    onOpenConfig() {}, onOpenArtifact() {},
  };
  try {
    await act(async () => root.render(React.createElement(RsiDetailHeader, props)));
    await act(async () => document.querySelector('[data-testid="rsi-action-install"]').click());
    assert.deepEqual(calls, [
      ['install', 'task-1'], ['installed', 'task-1'], ['refresh', 'local', { silent: true }],
    ]);
    calls.length = 0;
    globalThis.rsiInstallProbe.install = async () => { throw new Error('activation failed'); };
    await act(async () => document.querySelector('[data-testid="rsi-action-install"]').click());
    assert.deepEqual(calls, []);
    assert.ok(document.querySelector('[role="alert"]'));
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    delete globalThis.rsiInstallProbe;
    delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    delete globalThis.window;
    delete globalThis.document;
  }
});
