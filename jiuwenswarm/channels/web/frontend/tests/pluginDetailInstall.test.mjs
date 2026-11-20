import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test, { after } from 'node:test';
import { build } from 'esbuild';
import React, { act } from 'react';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
import { JSDOM } from 'jsdom';

await build({
  stdin: {
    contents: `export { PluginDetailPage } from './src/components/ConnectorMarket/PluginDetailPage';
      export { usePluginPackageStore } from './src/stores/pluginPackageStore';`,
    resolveDir: process.cwd(),
    loader: 'tsx',
  },
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/plugin-detail-install.mjs',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{}' },
  plugins: [{
    name: 'plugin-detail-install-boundaries',
    setup(builder) {
      // Exercise the real detail, store and API without unrelated UI exports.
      builder.onResolve({ filter: /^\.\.\/ui$/ }, () => ({ path: 'ui', namespace: 'detail-ui' }));
      builder.onLoad({ filter: /.*/, namespace: 'detail-ui' }, () => ({
        contents: ['PageCard', 'EntityHeader', 'DetailPromptChip', 'DetailSection']
          .map(name => `export { ${name} } from ${JSON.stringify(resolve(`src/components/ui/${name}/${name}.tsx`))};`).join('\n'),
        loader: 'js',
        resolveDir: process.cwd(),
      }));
      const mocks = {
        './webClient': 'export const webRequest = (...args) => globalThis.pluginDetailRequest(...args);',
        '../../stores/connectorStore': `export const useConnectorStore = selector => selector(globalThis.detailConnectorState);
          useConnectorStore.getState = () => globalThis.detailConnectorState;`,
        '../features/catalogCache': `export const catalogScope = () => 'test';
          export const scheduleCatalogRefresh = () => {};
          export const withCatalogCache = items => items;`,
        '../i18n': 'export default { t: key => key };',
      };
      builder.onResolve({ filter: /.*/ }, ({ path }) => mocks[path]
        ? { path, namespace: 'detail-mock' } : undefined);
      builder.onLoad({ filter: /.*/, namespace: 'detail-mock' }, ({ path }) => ({
        contents: mocks[path], loader: 'js', resolveDir: process.cwd(),
      }));
      builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'detail-svg' }));
      builder.onLoad({ filter: /.*/, namespace: 'detail-svg' }, () => ({
        contents: 'export default function SvgStub() { return null; }', loader: 'js',
      }));
      builder.onResolve({ filter: /^\/logo\.svg$/ }, () => ({ path: 'logo.svg', namespace: 'detail-url' }));
      builder.onLoad({ filter: /.*/, namespace: 'detail-url' }, () => ({
        contents: 'export default "logo.svg";', loader: 'js',
      }));
    },
  }],
});

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
const previousGlobals = new Map();
for (const [key, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  localStorage: dom.window.localStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  previousGlobals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
const { createRoot } = await import('react-dom/client');
after(() => {
  dom.window.close();
  for (const [key, descriptor] of previousGlobals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
  delete globalThis.pluginDetailRequest;
  delete globalThis.detailConnectorState;
});

await i18next.use(initReactI18next).init({
  lng: 'zh',
  showSupportNotice: false,
  resources: { zh: {
    translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8')),
  } },
  interpolation: { escapeValue: false },
});
const { PluginDetailPage, usePluginPackageStore: store } = await import('../node_modules/.cache/plugin-detail-install.mjs');
const pluginId = 'detail-plugin';

function setup({ pending = false, failure = false, source = 'local', quickInputs, installedQuickInputs } = {}) {
  const probe = { installed: false, connected: !pending, calls: [], cacheCleared: false };
  const connector = { name: 'crm', id: 'crm', runtimePackageName: 'crm', source: 'local', installed: true };
  globalThis.detailConnectorState = {
    connectors: [connector], builtinConnectors: [], myConnectors: [connector],
    connect: async name => {
      probe.calls.push(['connect', name]);
      probe.connected = true;
      return { type: 'connected' };
    },
  };
  globalThis.pluginDetailRequest = async (method, params) => {
    probe.calls.push([method, params]);
    if (method === 'plugin_packages.install') {
      if (failure) throw new Error('Plugin installation failed');
      if (!probe.connected) throw { payload: { pending_connectors: ['crm'] } };
      probe.installed = true;
      return {};
    }
    if (method === 'plugin_packages.show') return { package: {
      id: pluginId, packageName: pluginId, source,
      displayName: { zh: '详情插件', en: 'Detail plugin' },
      displayDescription: { zh: '实际服务返回详情', en: 'Server detail' },
      installed: probe.installed,
      connection_state: probe.installed && probe.connected ? 'connected' : 'disconnected',
      tags: [], skills: [], tools: [], rails: [], mcps: [],
      pending_connectors: probe.connected ? [] : ['crm'],
      quickInputs: probe.installed ? installedQuickInputs : quickInputs,
    } };
    if (method === 'plugin_packages.list') return { packages: [] };
    throw new Error(`Unexpected RPC: ${method}`);
  };
  store.setState({
    packages: [], localPackages: [], detailCache: {}, installed: { [pluginId]: false },
    connectionStateMap: {}, installingIds: {}, installPendingMap: {},
    replacement: null, replacementRetries: {}, error: null, successMessage: null, busyId: null,
  });
  return probe;
}

async function renderDetail(fromMy, check, onUseExample) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  try {
    await act(async () => root.render(React.createElement(PluginDetailPage, {
      id: pluginId, fromMy, onBack() {}, onUseExample,
    })));
    await check(host);
  } finally {
    await act(async () => root.unmount());
    host.remove();
  }
}

for (const pending of [false, true]) {
  test(`detail ${pending ? 'connector resume' : 'direct installation'} reloads the cache after installation`, async () => {
    const probe = setup({ pending });
    const unsubscribe = store.subscribe(state => {
      if (probe.installed && !state.detailCache[pluginId]) probe.cacheCleared = true;
    });
    try {
      await renderDetail(false, async host => {
        await act(async () => host.querySelector('[data-testid="connector-market-plugin-detail-install"]').click());
        assert.equal(probe.cacheCleared, true, 'real installation invalidates detail cache');
        assert.ok(host.querySelector('[data-testid="connector-market-plugin-detail"]'));
        assert.equal(host.querySelector('[data-testid="connector-market-plugin-detail-state"]'), null);
        assert.equal(host.querySelector('[data-testid="connector-market-plugin-detail-use"]'), null);
        assert.equal(host.querySelector('[data-testid="connector-market-plugin-detail-install"]'), null);
        assert.equal(store.getState().detailCache[pluginId].installed, true);
        const installCalls = probe.calls.filter(([method]) => method === 'plugin_packages.install');
        assert.equal(installCalls.length, pending ? 2 : 1);
        assert.equal(probe.calls.filter(([method]) => method === 'connect').length, pending ? 1 : 0);
        const lastInstall = probe.calls.findLastIndex(([method]) => method === 'plugin_packages.install');
        assert.ok(probe.calls.slice(lastInstall + 1).some(([method]) => method === 'plugin_packages.show'));
      });
      await renderDetail(true, async host => {
        assert.equal(host.querySelector('[data-testid="connector-market-plugin-detail-install"]'), null);
        assert.equal(host.querySelector('[data-testid="connector-market-plugin-detail-use"]'), null);
      });
    } finally {
      unsubscribe();
    }
  });
}

test('Hub plugin examples display before installation and keep localized prompts after installation', async () => {
  const prompts = ['检查当前差异', '找出测试缺口', '总结行动项'];
  const localized = { zh: '检查当前差异', en: 'Review the current diff' };
  const probe = setup({ source: 'hub', quickInputs: prompts, installedQuickInputs: [localized] });
  const used = [];
  const examples = host => Array.from(host.querySelectorAll('[data-testid="connector-market-plugin-detail-example"]'));
  try {
    await renderDetail(false, async host => {
      assert.deepEqual(examples(host).map(chip => chip.textContent), prompts);
      assert.deepEqual(examples(host).map(chip => chip.tagName), ['SPAN', 'SPAN', 'SPAN']);
      assert.equal(probe.calls.some(([method]) => method === 'plugin_packages.install'), false);
      assert.deepEqual(used, []);

      await act(async () => i18next.changeLanguage('en'));
      assert.deepEqual(examples(host).map(chip => chip.textContent), prompts);

      await act(async () => host.querySelector('[data-testid="connector-market-plugin-detail-install"]').click());
      const [example] = examples(host);
      assert.equal(example.tagName, 'BUTTON');
      assert.equal(example.textContent, localized.en);
      await act(async () => example.click());
      assert.deepEqual(used, [[localized.en, pluginId]]);
    }, (...args) => used.push(args));
  } finally {
    await i18next.changeLanguage('zh');
  }
});

test('failed installation preserves details and permits retry', async () => {
  const probe = setup({ failure: true });
  await renderDetail(false, async host => {
    await act(async () => host.querySelector('[data-testid="connector-market-plugin-detail-install"]').click());
    assert.equal(probe.installed, false);
    assert.ok(host.querySelector('[data-testid="connector-market-plugin-detail"]'));
    assert.equal(host.querySelector('[data-testid="connector-market-plugin-detail-state"]'), null);
    assert.equal(host.querySelector('[data-testid="connector-market-plugin-detail-install"]').disabled, false);
    assert.equal(host.querySelector('[data-testid="connector-market-plugin-detail-use"]'), null);
    assert.equal(store.getState().error, 'Plugin installation failed');
  });
});
