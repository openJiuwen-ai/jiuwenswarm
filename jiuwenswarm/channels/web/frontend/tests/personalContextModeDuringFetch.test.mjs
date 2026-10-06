import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { build } from 'esbuild';

const frontendRoot = fileURLToPath(new URL('../', import.meta.url));
const output = fileURLToPath(new URL('../node_modules/.cache/personal-context-mode-during-fetch/SettingsPanel.mjs', import.meta.url));

await build({
  entryPoints: [fileURLToPath(new URL('../src/components/PersonalContext/SettingsPanel.tsx', import.meta.url))],
  outfile: output,
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  loader: { '.css': 'empty' },
  plugins: [{
    name: 'isolate-personal-context-settings',
    setup(builder) {
      const dependencies = new Map([
        ['../../stores', `
          import { useSyncExternalStore } from 'react';
          export const usePersonalContextStore = () => useSyncExternalStore(globalThis.__pcSubscribe, globalThis.__pcSnapshot);
          usePersonalContextStore.getState = () => globalThis.__pcSnapshot();
          export const useSessionStore = (selector) => selector({ availableModels: [] });
        `],
        ['../../services/personalContextApi', `
          export const STRATEGY_OPTIONS = ['agent', 'balanced', 'rules'];
          export const hasRunningFetchTask = (status) => Object.values(status?.fetch_run_progress ?? {}).some(
            (item) => item.run_state === 'running' || item.run_state === 'stopping'
          );
          export const isFetchTaskRunningError = () => false;
          export const pcApi = { getStatus: async () => globalThis.__pcBackendStatus };
        `],
        ['../Switch', 'export const Switch = () => null;'],
        ['../ModelPicker', 'export default function ModelPicker() { return null; }'],
        ['../../features/settings/components/SettingRow', `
          import { createElement } from 'react';
          export const SettingRow = ({ title, description, children }) => createElement('div', null, title, description, children);
        `],
        ['../../components/ui/Toast/toastStore', 'export const toast = { open() {} };'],
        ['react-i18next', 'export const useTranslation = () => ({ t: (key) => key });'],
      ]);
      builder.onResolve({ filter: /\.(svg|png)$/ }, () => ({ path: 'asset', namespace: 'pc-mode-test' }));
      builder.onResolve({ filter: /^react$/ }, () => ({ path: 'react', external: true }));
      builder.onResolve({ filter: /.*/ }, (args) =>
        dependencies.has(args.path) ? { path: args.path, namespace: 'pc-mode-test' } : null);
      builder.onLoad({ filter: /.*/, namespace: 'pc-mode-test' }, (args) => ({
        contents: args.path === 'asset' ? 'export default "";' : dependencies.get(args.path),
        loader: 'js',
      }));
    },
  }],
  absWorkingDir: frontendRoot,
});

const { PersonalContextSettingsPanel } = await import(
  new URL('../node_modules/.cache/personal-context-mode-during-fetch/SettingsPanel.mjs', import.meta.url)
);

test('mode is blocked while a fetch is stopping, then enabled after status polling', async () => {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  const globals = {
    window: dom.window,
    document: dom.window.document,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const originals = new Map(Object.keys(globals).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const [key, value] of Object.entries(globals)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const intervals = new Map();
  dom.window.setInterval = (callback, ms) => {
    const id = intervals.size + 1;
    intervals.set(id, { callback, ms });
    return id;
  };
  dom.window.clearInterval = (id) => intervals.delete(id);

  const stopping = { configured: true, fetch_run_progress: { notes: { run_state: 'stopping' } } };
  const idle = { configured: true, fetch_run_progress: { notes: { run_state: 'idle' } } };
  const listeners = new Set();
  let state;
  let statusReads = 0;
  globalThis.__pcBackendStatus = stopping;
  globalThis.__pcSubscribe = (listener) => { listeners.add(listener); return () => listeners.delete(listener); };
  globalThis.__pcSnapshot = () => state;
  const update = (patch) => {
    state = { ...state, ...patch };
    for (const listener of listeners) listener();
  };
  state = {
    config: { configured: true, collection_enabled: true, agent_use_enabled: true, strategy_profile: 'agent', model_index: null },
    status: stopping,
    loadingConfig: false,
    pendingWrites: {},
    configNeedsReconciliation: false,
    authByProvider: {},
    loadAll: async () => {},
    loadStatus: async () => { statusReads += 1; update({ status: globalThis.__pcBackendStatus }); },
    loadAuthStatus: async () => {},
    setMasterEnabled: async () => {},
    setEnabled: async () => {},
    setStrategyProfile: async () => {},
    selectModel: async () => {},
    authorizeProvider: async () => {},
  };

  const root = createRoot(dom.window.document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(PersonalContextSettingsPanel, { isConnected: true })));
    const select = dom.window.document.querySelector('select');
    assert.equal(select.disabled, true);
    assert.match(dom.window.document.body.textContent, /personalContext\.settings\.strategyLockedByFetch/);

    globalThis.__pcBackendStatus = idle;
    const poll = [...intervals.values()].find(({ ms }) => ms === 5000);
    assert.ok(poll, 'settings page should poll fetch status');
    await act(async () => poll.callback());
    assert.ok(statusReads > 0);
    assert.equal(select.disabled, false);
  } finally {
    await act(async () => root.unmount());
    for (const key of ['__pcBackendStatus', '__pcSubscribe', '__pcSnapshot']) delete globalThis[key];
    for (const [key, original] of originals) {
      if (original) Object.defineProperty(globalThis, key, original);
      else delete globalThis[key];
    }
    dom.window.close();
  }
});
