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
    'src/components/ConnectorMarket/SystemPluginDetailPage.tsx',
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
          ? [
            ...['PageCard', 'EntityHeader', 'DetailPromptChip', 'DetailSection', 'PageToolbarSearch', 'PageHeader', 'CategoryTabs', 'Tabs', 'EmptyState', 'Button', 'Dialog', 'CloseButton', 'MarkdownPane', 'PageToolbar', 'Select']
              .map(name => `export { ${name} } from ${JSON.stringify(resolve(`src/components/ui/${name}/${name}.tsx`))};`),
            `export { FilePreviewContent } from ${JSON.stringify(resolve('src/components/ui/FilePreviewPanel/FilePreviewContent.tsx'))};`,
            `export { FilePreviewPanel } from ${JSON.stringify(resolve('src/components/ui/FilePreviewPanel/FilePreviewPanel.tsx'))};`,
            `export { FilePreviewTree, findDefaultPreviewFile } from ${JSON.stringify(resolve('src/components/ui/FilePreviewPanel/FilePreviewTree.tsx'))};`,
            `export { getPreviewFileLabel } from ${JSON.stringify(resolve('src/components/ui/FilePreviewPanel/filePreviewShared.ts'))};`,
          ].join('\n')
          : `export { useHorizontalScrollEdges } from ${JSON.stringify(resolve('src/hooks/useHorizontalScrollEdges.ts'))};`,
        loader: 'js',
        resolveDir: process.cwd(),
      }));
      builder.onResolve({ filter: /stores\/(pluginPackageStore|connectorStore|systemPluginStore)$/ }, ({ path }) => ({
        path: path.endsWith('pluginPackageStore') ? 'plugin' : path.endsWith('systemPluginStore') ? 'system' : 'connector',
        namespace: 'action-store',
      }));
      builder.onLoad({ filter: /.*/, namespace: 'action-store' }, ({ path }) => ({
        contents: path === 'plugin'
          ? 'export const usePluginPackageStore = selector => selector(globalThis.pluginActionState); usePluginPackageStore.getState = () => globalThis.pluginActionState;'
          : path === 'system'
            ? 'export const useSystemPluginStore = selector => selector(globalThis.systemPluginActionState); useSystemPluginStore.getState = () => globalThis.systemPluginActionState;'
            : 'export const useConnectorStore = selector => selector(globalThis.connectorActionState); useConnectorStore.getState = () => globalThis.connectorActionState;',
        loader: 'js',
      }));
      builder.onResolve({ filter: /services\/(pluginPackagesApi|connectorApi|extensionsV2Api)$/ }, ({ path }) => ({
        path: path.endsWith('pluginPackagesApi')
          ? 'pluginPackagesApi'
          : path.endsWith('extensionsV2Api')
            ? 'extensionsV2Api'
            : 'connectorApi',
        namespace: 'action-api',
      }));
      builder.onLoad({ filter: /.*/, namespace: 'action-api' }, ({ path }) => ({
        contents: path === 'extensionsV2Api'
          ? 'export const systemPluginApi = { catalog: (...args) => globalThis.systemPluginActionApi.catalog(...args) };'
          : `export const ${path} = {};`,
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
  delete globalThis.systemPluginActionState;
  delete globalThis.systemPluginActionApi;
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
const { SystemPluginDetailPage } = await import('../node_modules/.cache/plugin-mine-actions/SystemPluginDetailPage.mjs');
const plugin = {
  id: 'legacy-plugin', runtimePackageName: 'legacy-plugin', source: 'local',
  displayName: { zh: '本地插件', en: 'Local plugin' },
  displayDescription: { zh: '本地描述', en: 'Local description' },
  installed: false, connectionState: 'disconnected', category: '',
  tags: [], skills: [], tools: [], rails: [], mcps: [],
};

function resetState({ installed = false, connected = false } = {}) {
  const calls = { install: [], connect: [], use: [], detail: [], systemInstall: [] };
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
  globalThis.systemPluginActionState = {
    catalog: [], mine: [], detailCache: {}, busyIds: {}, loading: false,
    loadDetail: async () => true,
    install: async id => { calls.systemInstall.push(id); return { ok: true }; },
    uninstall: async () => ({ ok: true }),
    reload: async () => ({ ok: true }),
    setEnabled: async () => ({ ok: true }),
  };
  globalThis.systemPluginActionApi = { catalog: async () => globalThis.systemPluginActionState.mine };
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

function marketProps(calls, topTab = 'my', myKind = 'plugin', pluginKind = 'agent') {
  return {
    topTab, myKind, pluginKind,
    onTopTabChange() {}, onMyKindChange() {}, onPluginKindChange() {},
    onOpenConnectorDetail() {}, onCreateManual() {}, onCreateWithSkill() {},
    onCreateWithUpload() {}, onRegisterCustomMcp() {},
    officeSuites: null, onOpenOfficeSuite() {}, onOpenSystemPluginDetail() {},
    onRefresh: async () => {}, refreshNonce: 0,
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

for (const [surface, props] of [
  ['my extensions', calls => marketProps(calls)],
  ['plugin marketplace', calls => marketProps(calls, 'plugin', 'plugin', 'system')],
]) {
  test(`${surface} system plugin toggles do not replace the card action with installing copy`, async () => {
    await i18next.changeLanguage('zh');
    const calls = resetState();
    const systemPlugin = {
      id: 'token-weather', packageId: 'token-weather', version: '1.0.0', installedVersion: '1.0.0',
      displayName: { zh: 'Token 天气', en: 'Token Weather' },
      displayDescription: { zh: '系统插件', en: 'System plugin' },
      tags: [], avatar: null, installed: true, active: true, enabled: true,
    };
    globalThis.systemPluginActionState.mine = [systemPlugin];
    globalThis.systemPluginActionState.catalog = [systemPlugin];
    globalThis.systemPluginActionState.busyIds = { 'token-weather': true };

    await render(MarketplacePage, props(calls), async host => {
      assert.ok(host.querySelector('[data-testid="connector-market-system-plugin-enabled"]'));
      assert.doesNotMatch(host.textContent, /安装中/);
      assert.equal(host.querySelector('[data-testid="connector-market-card-install"]'), null);
    });
  });
}

test('system plugin marketplace install confirms when another plugin is enabled', async () => {
  await i18next.changeLanguage('zh');
  const calls = resetState();
  globalThis.systemPluginActionState.catalog = [{
    id: 'target-asset', packageId: 'target-plugin', version: '1.0.0', installedVersion: '',
    displayName: { zh: '目标插件', en: 'Target plugin' },
    displayDescription: { zh: '待安装', en: 'Not installed' },
    tags: [], avatar: null, installed: false, active: false, enabled: true,
  }];
  globalThis.systemPluginActionState.mine = [{
    id: 'active-plugin', packageId: 'active-plugin', version: '1.0.0', installedVersion: '1.0.0',
    displayName: { zh: '已开启插件', en: 'Enabled plugin' },
    displayDescription: { zh: '已开启', en: 'Enabled' },
    tags: [], avatar: null, installed: true, active: true, enabled: true,
  }];

  await render(MarketplacePage, marketProps(calls, 'plugin', 'plugin', 'system'), async host => {
    await act(async () => host.querySelector('[data-testid="connector-market-card-install"]').click());
    assert.deepEqual(calls.systemInstall, []);
    const dialog = document.querySelector('[data-testid="connector-market-confirm-dialog"]');
    assert.match(dialog.textContent, /开启多个系统插件/);
    await act(async () => dialog.querySelector('[data-testid="connector-market-confirm-ok"]').click());
    assert.deepEqual(calls.systemInstall, ['target-asset']);
  });
});

test('system plugin marketplace installs directly when no plugin is enabled', async () => {
  await i18next.changeLanguage('zh');
  const calls = resetState();
  globalThis.systemPluginActionState.catalog = [{
    id: 'target-asset', packageId: 'target-plugin', version: '1.0.0', installedVersion: '',
    displayName: { zh: '目标插件', en: 'Target plugin' },
    displayDescription: { zh: '待安装', en: 'Not installed' },
    tags: [], avatar: null, installed: false, active: false, enabled: true,
  }];

  await render(MarketplacePage, marketProps(calls, 'plugin', 'plugin', 'system'), async host => {
    await act(async () => host.querySelector('[data-testid="connector-market-card-install"]').click());
    assert.deepEqual(calls.systemInstall, ['target-asset']);
    assert.equal(document.querySelector('[data-testid="connector-market-confirm-dialog"]'), null);
  });
});

test('system plugin marketplace install continues when conflict discovery fails', async () => {
  const calls = resetState();
  globalThis.systemPluginActionState.catalog = [{
    id: 'target-asset', packageId: 'target-plugin', version: '1.0.0', installedVersion: '',
    displayName: { zh: '目标插件', en: 'Target plugin' },
    displayDescription: { zh: '待安装', en: 'Not installed' },
    tags: [], avatar: null, installed: false, active: false, enabled: true,
  }];
  globalThis.systemPluginActionApi.catalog = async () => { throw new Error('catalog unavailable'); };

  await render(MarketplacePage, marketProps(calls, 'plugin', 'plugin', 'system'), async host => {
    await act(async () => host.querySelector('[data-testid="connector-market-card-install"]').click());
    assert.deepEqual(calls.systemInstall, ['target-asset']);
    assert.equal(document.querySelector('[data-testid="connector-market-confirm-dialog"]'), null);
  });
});

test('system plugin detail install continues when conflict discovery fails', async () => {
  const calls = resetState();
  globalThis.systemPluginActionState.detailCache['target-asset'] = {
    id: 'target-asset', assetId: 'target-asset', packageId: 'target-plugin', version: '1.0.0',
    versions: ['1.0.0'], installedVersion: '', displayName: { zh: '目标插件', en: 'Target plugin' },
    displayDescription: { zh: '待安装', en: 'Not installed' }, details: '', readme: '', tags: [],
    avatar: null, preview: null, events: [], installed: false, active: false, enabled: true,
    source: 'hub', origin: 'hub',
  };
  globalThis.systemPluginActionApi.catalog = async () => { throw new Error('catalog unavailable'); };

  await render(SystemPluginDetailPage, { id: 'target-asset', onBack() {} }, async host => {
    const install = [...host.querySelectorAll('button')].find(button => button.textContent === '安装');
    assert.ok(install);
    await act(async () => install.click());
    assert.deepEqual(calls.systemInstall, ['target-asset']);
    assert.equal(host.querySelector('[data-testid="connector-market-system-plugin-detail-error"]'), null);
  });
});

for (const fromMy of [true, false]) {
  test(`${fromMy ? 'my' : 'marketplace'} uninstalled plugin detail respects the page action scope`, async () => {
    const calls = resetState();
    await render(PluginDetailPage, { id: plugin.id, fromMy, onBack() {}, onUse() {} }, async host => {
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
