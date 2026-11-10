import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import { createInstance } from 'i18next';

const dom = new JSDOM('<div id="root"></div>');
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.localStorage = { getItem: () => null, setItem: () => {} };
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const translations = {
  'pluginReplacement.title': 'Plugin already exists',
  'pluginReplacement.message': 'Replace the local plugin? Local changes will be overwritten.',
  'pluginReplacement.confirm': 'Replace local version',
  'pluginReplacement.busy': 'Retry when idle',
};
const translate = (key) => translations[key] ?? key;
globalThis.pluginReplacementTranslate = translate;

await build({
  stdin: {
    contents: `export {usePluginPackageStore} from './src/stores/pluginPackageStore';
      export {PluginReplacementDialog} from './src/components/ConnectorMarket/PluginReplacementDialog';`,
    resolveDir: process.cwd(), loader: 'tsx',
  },
  outfile: 'node_modules/.cache/plugin-replacement.mjs',
  bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic',
  plugins: [{
    name: 'plugin-service-boundaries',
    setup(builder) {
      const mocks = {
        './webClient': 'export const webRequest = (...args) => globalThis.pluginReplacementRequest(...args);',
        '../features/catalogCache': `export const catalogScope=()=> 'test';
          export const scheduleCatalogRefresh=()=>{};
          export const withCatalogCache=(items)=>items;`,
        '../features/agentManagement/upload': 'export const extractRpcErrorMessage=String;',
        '../i18n': 'export default {t:(...args)=>globalThis.pluginReplacementTranslate(...args)};',
        'react-i18next': 'export const useTranslation=()=>({t:(...args)=>globalThis.pluginReplacementTranslate(...args)});',
      };
      builder.onResolve({ filter: /.*/ }, (args) => mocks[args.path]
        ? { path: args.path, namespace: 'replacement-mock' } : undefined);
      builder.onLoad({ filter: /.*/, namespace: 'replacement-mock' }, (args) => ({
        contents: mocks[args.path], loader: 'js', resolveDir: process.cwd(),
      }));
    },
  }],
});
const { usePluginPackageStore: store, PluginReplacementDialog } = await import('../node_modules/.cache/plugin-replacement.mjs');
const conflict = { package_id: 'ttse', local_version: '2.0', hub_version: '1.0', local_fingerprint: 'confirmed-hash' };
const calls = [];
const replies = [];
globalThis.pluginReplacementRequest = async (method, params) => {
  if (method !== 'plugin_packages.install') return { packages: [] };
  calls.push(params);
  const response = replies.shift();
  if (response) throw { payload: response };
};

function reset() {
  calls.length = 0;
  replies.length = 0;
  store.setState({ replacement: null, replacementRetries: {}, installingIds: {}, installed: {}, error: null });
}

test('conflict does not install; cancel sends no second request', async () => {
  reset();
  replies.push({ error_code: 'PLUGIN_LOCAL_CONFLICT', conflict });
  await store.getState().install('asset-ttse');
  assert.equal(store.getState().installed['asset-ttse'], undefined);
  assert.equal(store.getState().replacement.conflict.local_fingerprint, 'confirmed-hash');
  store.getState().cancelReplacement();
  assert.equal(store.getState().replacement, null);
  assert.equal(calls.length, 1);
});

test('shared dialog confirms the original fingerprint', async () => {
  reset();
  replies.push({ error_code: 'PLUGIN_LOCAL_CONFLICT', conflict });
  await store.getState().install('asset-ttse');
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(createElement(PluginReplacementDialog)));
  assert.match(document.body.textContent, /Local changes will be overwritten/);
  await act(async () => document.querySelector('[data-testid="connector-market-confirm-ok"]').click());
  assert.deepEqual(calls[1], { id: 'asset-ttse', replace_local: true, local_fingerprint: 'confirmed-hash' });
  assert.equal(store.getState().installed['asset-ttse'], true);
  assert.equal(store.getState().replacement, null);
  await act(async () => root.unmount());
});

test('busy replacement ends confirmation without reporting success', async () => {
  reset();
  replies.push({ error_code: 'PLUGIN_REPLACEMENT_BUSY' });
  await store.getState().install('asset-ttse', { replace_local: true, local_fingerprint: 'confirmed-hash' });
  assert.equal(store.getState().error, 'Retry when idle');
  assert.equal(store.getState().replacement, null);
  assert.equal(store.getState().installed['asset-ttse'], undefined);
});

test('connector resume retains consent and the original fingerprint', async () => {
  reset();
  replies.push({ pending_connectors: ['connector'] });
  const options = { replace_local: true, local_fingerprint: 'confirmed-hash' };
  await store.getState().install('asset-ttse', options);
  store.getState().clearInstallPending('asset-ttse', true);
  await store.getState().install('asset-ttse');
  assert.deepEqual(calls[1], { id: 'asset-ttse', ...options });
});

test('cancelling connector setup discards replacement consent', async () => {
  reset();
  replies.push({ pending_connectors: ['connector'] });
  await store.getState().install('asset-ttse', { replace_local: true, local_fingerprint: 'confirmed-hash' });
  store.getState().clearInstallPending('asset-ttse');
  replies.push({ error_code: 'PLUGIN_LOCAL_CONFLICT', conflict });
  await store.getState().install('asset-ttse');
  assert.deepEqual(calls[1], { id: 'asset-ttse' });
  assert.ok(store.getState().replacement);
});

test('dialog renders the shipped Chinese translations and version values', async () => {
  reset();
  const resources = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
  const instance = createInstance();
  await instance.init({ lng: 'zh', resources: { zh: { translation: resources } }, interpolation: { escapeValue: false } });
  globalThis.pluginReplacementTranslate = (...args) => instance.t(...args);
  store.setState({ replacement: { id: 'asset-ttse', conflict } });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(PluginReplacementDialog)));
    assert.match(document.body.textContent, /插件已存在/);
    assert.match(document.body.textContent, /本地已存在插件 ttse（版本 2.0）。是否使用广场版本 1.0 替换？/);
    assert.equal(document.querySelector('[data-testid="connector-market-confirm-ok"]').textContent, '替换本地版本');
    assert.equal(document.querySelector('[data-testid="connector-market-confirm-cancel"]').textContent, '取消');
    assert.doesNotMatch(document.body.textContent, /\?{2,}|\uFFFD/);
    for (const value of Object.values(resources.pluginReplacement)) {
      assert.doesNotMatch(value, /\?{2,}|\uFFFD/);
    }
  } finally {
    await act(async () => root.unmount());
    globalThis.pluginReplacementTranslate = translate;
  }
});
