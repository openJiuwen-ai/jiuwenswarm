import assert from 'node:assert/strict';
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';
import test, { after } from 'node:test';
import { build } from 'esbuild';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';
import { JSDOM } from 'jsdom';

await build({
  entryPoints: [
    'src/components/ConnectorMarket/MarketplacePage.tsx',
    'src/components/ConnectorMarket/PluginDetailPage.tsx',
  ],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/plugin-mine-actions',
  outExtension: { '.js': '.mjs' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  define: { 'import.meta.env': '{}' },
  plugins: [{
    name: 'plugin-action-test-boundaries',
    setup(builder) {
      // Keep real UI components without initializing unrelated barrel exports.
      builder.onResolve({ filter: /^\.\.\/ui$/ }, () => ({ path: 'ui', namespace: 'action-exports' }));
      builder.onResolve({ filter: /^(\.\.\/)+hooks$/ }, () => ({ path: 'hooks', namespace: 'action-exports' }));
      builder.onLoad({ filter: /.*/, namespace: 'action-exports' }, ({ path }) => ({
        contents: path === 'ui'
          ? ['PageCard', 'EntityHeader', 'DetailPromptChip', 'DetailSection', 'PageToolbarSearch', 'PageHeader', 'CategoryTabs', 'Tabs', 'EmptyState']
            .map(name => `export { ${name} } from ${JSON.stringify(resolve(`src/components/ui/${name}/${name}.tsx`))};`).join('\n')
          : `export { useHorizontalScrollEdges } from ${JSON.stringify(resolve('src/hooks/useHorizontalScrollEdges.ts'))};`,
        loader: 'js',
        resolveDir: process.cwd(),
      }));
      builder.onResolve({ filter: /stores\/(pluginPackageStore|connectorStore)$/ }, ({ path }) => ({
        path: path.endsWith('pluginPackageStore') ? 'plugin' : 'connector',
        namespace: 'action-store',
      }));
      builder.onLoad({ filter: /.*/, namespace: 'action-store' }, ({ path }) => ({
        contents: path === 'plugin'
          ? 'export const usePluginPackageStore = selector => selector(globalThis.pluginActionState); usePluginPackageStore.getState = () => globalThis.pluginActionState;'
          : 'export const useConnectorStore = selector => selector(globalThis.connectorActionState); useConnectorStore.getState = () => globalThis.connectorActionState;',
        loader: 'js',
      }));
      builder.onResolve({ filter: /services\/(pluginPackagesApi|connectorApi)$/ }, ({ path }) => ({
        path: path.endsWith('pluginPackagesApi') ? 'pluginPackagesApi' : 'connectorApi',
        namespace: 'action-api',
      }));
      builder.onLoad({ filter: /.*/, namespace: 'action-api' }, ({ path }) => ({
        contents: `export const ${path} = {};`,
        loader: 'js',
      }));
      builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'action-svg' }));
      builder.onLoad({ filter: /.*/, namespace: 'action-svg' }, () => ({
        contents: 'export default function SvgStub() { return null; }',
        loader: 'js',
      }));
      builder.onResolve({ filter: /^\/logo\.svg$/ }, () => ({ path: 'logo.svg', namespace: 'action-url' }));
      builder.onLoad({ filter: /.*/, namespace: 'action-url' }, () => ({
        contents: 'export default "logo.svg";',
        loader: 'js',
      }));
    },
  }],
});

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
const previousGlobals = new Map();
for (const [key, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  previousGlobals.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => {
  for (const [key, descriptor] of previousGlobals) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
  delete globalThis.pluginActionState;
  delete globalThis.connectorActionState;
  dom.window.close();
});

await i18next.use(initReactI18next).init({
  lng: 'zh',
  showSupportNotice: false,
  resources: Object.fromEntries(['zh', 'en'].map(language => [language, {
    translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')),
  }])),
  interpolation: { escapeValue: false },
});

const { MarketplacePage } = await import('../node_modules/.cache/plugin-mine-actions/MarketplacePage.mjs');
const { PluginDetailPage } = await import('../node_modules/.cache/plugin-mine-actions/PluginDetailPage.mjs');
const plugin = {
  id: 'legacy-plugin', runtimePackageName: 'legacy-plugin', source: 'local',
  displayName: { zh: '本地插件', en: 'Local plugin' },
  displayDescription: { zh: '本地描述', en: 'Local description' },
  installed: false, connectionState: 'disconnected', category: '',
  tags: [], skills: [], tools: [], rails: [], mcps: [],
};

function resetState({ installed = false, connected = false } = {}) {
  const calls = { install: [], connect: [], use: [], detail: [] };
  globalThis.pluginActionState = {
    packages: [plugin], localPackages: [plugin],
    installed: { [plugin.id]: installed },
    connectionStateMap: { [plugin.id]: connected ? 'connected' : 'disconnected' },
    installingIds: {}, installPendingMap: {},
    detailCache: { [plugin.id]: { ...plugin, installed } },
    loadDetail: async () => true,
    install: async id => { calls.install.push(id); },
    clearInstallPending() {},
  };
  const connector = {
    id: 'my-mcp', runtimePackageName: 'my-mcp', displayName: 'My MCP',
    category: '', installed: true, connectionState: 'disconnected',
  };
  globalThis.connectorActionState = {
    connectors: [connector], builtinConnectors: [], myConnectors: [connector], busyMap: {},
    connect: async name => { calls.connect.push(name); return null; },
  };
  return calls;
}

async function render(component, props, check) {
  const host = document.createElement('div');
  document.body.appendChild(host);
  const root = createRoot(host);
  try {
    await act(async () => root.render(React.createElement(component, props)));
    await check(host);
  } finally {
    await act(async () => root.unmount());
    host.remove();
  }
}

function marketProps(calls, topTab = 'my', myKind = 'plugin') {
  return {
    topTab, myKind,
    onTopTabChange() {}, onMyKindChange() {},
    onOpenConnectorDetail() {}, onCreateManual() {}, onCreateWithSkill() {},
    onCreateWithUpload() {}, onRegisterCustomMcp() {},
    onOpenPluginDetail: id => { calls.detail.push(id); },
    onUse: payload => { calls.use.push(payload); },
  };
}

for (const [language, useLabel] of [['zh', '使用'], ['en', 'Use']]) {
  test(`my uninstalled plugin offers use and opens details without installing (${language})`, async () => {
    await i18next.changeLanguage(language);
    const calls = resetState();
    await render(MarketplacePage, marketProps(calls), async host => {
      assert.equal(host.querySelector('[data-testid="connector-market-card-install"]'), null);
      const use = host.querySelector('[data-testid="connector-market-card-use"]');
      assert.equal(use.textContent, useLabel);
      if (language === 'zh') {
        writeFileSync(new URL('../node_modules/.cache/plugin-mine-actions/my-plugin.html', import.meta.url), host.outerHTML);
      }
      await act(async () => use.click());
      assert.deepEqual(calls.detail, [plugin.id]);
      assert.deepEqual(calls.install, []);
      assert.deepEqual(calls.use, []);
    });
  });
}

test('my ready plugin uses its runtime name without installing', async () => {
  const calls = resetState({ installed: true, connected: true });
  await render(MarketplacePage, marketProps(calls), async host => {
    await act(async () => host.querySelector('[data-testid="connector-market-card-use"]').click());
    assert.deepEqual(calls.use, [{ kind: 'plugin', id: plugin.runtimePackageName }]);
    assert.deepEqual(calls.install, []);
  });
});

test('my installed plugin with disconnected dependencies opens details without reinstalling', async () => {
  const calls = resetState({ installed: true });
  await render(MarketplacePage, marketProps(calls), async host => {
    assert.equal(host.querySelector('[data-testid="connector-market-card-install"]'), null);
    await act(async () => host.querySelector('[data-testid="connector-market-card-use"]').click());
    assert.deepEqual(calls.detail, [plugin.id]);
    assert.deepEqual(calls.install, []);
    assert.deepEqual(calls.use, []);
  });
});

test('plugin marketplace retains the install action', async () => {
  const calls = resetState();
  await render(MarketplacePage, marketProps(calls, 'plugin'), async host => {
    const install = host.querySelector('[data-testid="connector-market-card-install"]');
    assert.ok(install);
    await act(async () => install.click());
    assert.deepEqual(calls.install, [plugin.id]);
    assert.deepEqual(calls.detail, []);
  });
});

test('my installed disconnected MCP retains the connect action', async () => {
  await i18next.changeLanguage('zh');
  const calls = resetState();
  await render(MarketplacePage, marketProps(calls, 'my', 'mcp'), async host => {
    const connect = host.querySelector('[data-testid="connector-market-card-install"]');
    assert.equal(connect.textContent, '连接');
    await act(async () => connect.click());
    assert.deepEqual(calls.connect, ['my-mcp']);
    assert.deepEqual(calls.install, []);
  });
});

for (const fromMy of [true, false]) {
  test(`${fromMy ? 'my' : 'marketplace'} uninstalled plugin detail respects the page action scope`, async () => {
    const calls = resetState();
    await render(PluginDetailPage, { id: plugin.id, fromMy, onBack() {} }, async host => {
      const install = host.querySelector('[data-testid="connector-market-plugin-detail-install"]');
      const use = host.querySelector('[data-testid="connector-market-plugin-detail-use"]');
      if (fromMy) {
        assert.equal(install, null);
        assert.ok(use);
        assert.equal(use.disabled, true);
        await act(async () => use.click());
        assert.deepEqual(calls.install, []);
      } else {
        assert.equal(use, null);
        assert.ok(install);
        await act(async () => install.click());
        assert.deepEqual(calls.install, [plugin.id]);
      }
    });
  });
}

test('my ready plugin detail can use the installed package', async () => {
  const calls = resetState({ installed: true, connected: true });
  await render(PluginDetailPage, {
    id: plugin.id, fromMy: true, onBack() {},
    onUse: id => { calls.use.push(id); },
  }, async host => {
    assert.equal(host.querySelector('[data-testid="connector-market-plugin-detail-install"]'), null);
    const use = host.querySelector('[data-testid="connector-market-plugin-detail-use"]');
    assert.equal(use.disabled, false);
    await act(async () => use.click());
    assert.deepEqual(calls.use, [plugin.runtimePackageName]);
    assert.deepEqual(calls.install, []);
  });
});
