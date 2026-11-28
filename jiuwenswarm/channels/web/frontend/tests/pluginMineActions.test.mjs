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
    'src/components/ConnectorMarket/UploadFileCreateModal.tsx',
    'src/multi-session/state/createConversationSession.ts',
  ],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outdir: 'node_modules/.cache/plugin-mine-actions',
  entryNames: '[name]',
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
          : [
            `export { useHorizontalScrollEdges } from ${JSON.stringify(resolve('src/hooks/useHorizontalScrollEdges.ts'))};`,
            'export const useDesktopLocalFilePickerReady = () => false;',
          ].join('\n'),
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
          ? `export const EXTENSION_PACKAGE_BUSY = 'EXTENSION_PACKAGE_BUSY';
          export const systemPluginApi = {
            catalog: (...args) => globalThis.systemPluginActionApi.catalog(...args),
            inspectImportPath: (...args) => globalThis.systemPluginActionApi.inspectImportPath(...args),
          };`
          : `export const ${path} = {};`,
        loader: 'js',
      }));
      builder.onResolve({ filter: /ui\/Toast\/toastStore$/ }, () => ({ path: 'toastStore', namespace: 'action-toast' }));
      builder.onLoad({ filter: /.*/, namespace: 'action-toast' }, () => ({
        contents: [
          'const record = config => { (globalThis.connectorToastCalls ||= []).push(config); return 0; };',
          'export const toast = { open: record, close() {}, closeAll() {} };',
          'export const toastStore = { open() {}, close() {}, closeAll() {}, subscribe: () => () => {}, getSnapshot: () => [] };',
        ].join('\n'),
        loader: 'js',
      }));
      builder.onResolve({ filter: /features\/workspace\/localFilePicker$/ }, () => ({
        path: 'localFilePicker', namespace: 'action-file-picker',
      }));
      builder.onLoad({ filter: /.*/, namespace: 'action-file-picker' }, () => ({
        contents: `
          export const DESKTOP_FILE_DRAG_EVENT = 'jiuwen-desktop-file-drag';
          export const selectLocalFiles = () => ({ ok: true, files: [globalThis.pluginActionFilePick] });
          export const registerDesktopLocalFilesConsumer = () => () => {};
        `,
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
  delete globalThis.connectorToastCalls;
  delete globalThis.pluginActionFilePick;
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
const { UploadFileCreateModal } = await import('../node_modules/.cache/plugin-mine-actions/UploadFileCreateModal.mjs');
const { createConversationSession } = await import('../node_modules/.cache/plugin-mine-actions/createConversationSession.mjs');
const plugin = {
  id: 'legacy-plugin', runtimePackageName: 'legacy-plugin', source: 'local',
  displayName: { zh: '本地插件', en: 'Local plugin' },
  displayDescription: { zh: '本地描述', en: 'Local description' },
  installed: false, connectionState: 'disconnected', category: '',
  tags: [], skills: [], tools: [], rails: [], mcps: [],
};

function resetState({ installed = false, connected = false } = {}) {
  const calls = { install: [], connect: [], use: [], detail: [], systemInstall: [] };
  globalThis.connectorToastCalls = [];
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
    catalog: [], mine: [], detailCache: {}, busyIds: {}, installingIds: {}, loading: false,
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

for (const [packageId, filename] of [['target-plugin', 'system-plugin.zip'], [null, 'agent-plugin.zip']]) {
  test(`ZIP upload checks for enabled system plugins (${filename})`, async () => {
    await i18next.changeLanguage('zh');
    resetState();
    globalThis.systemPluginActionState.mine = [{
      id: 'enabled-plugin', packageId: 'enabled-plugin', installed: true, enabled: true,
    }];
    const path = `/tmp/${filename}`;
    globalThis.pluginActionFilePick = { path, filename, size: 100, mime_type: 'application/zip', kind: 'document' };
    const inspected = [];
    globalThis.systemPluginActionApi.inspectImportPath = async filePath => {
      inspected.push(filePath);
      return packageId;
    };
    const catalog = globalThis.systemPluginActionApi.catalog;
    const catalogCalls = [];
    globalThis.systemPluginActionApi.catalog = async (...args) => {
      catalogCalls.push(args);
      return catalog(...args);
    };
    const imported = [];
    await render(UploadFileCreateModal, {
      onCancel() {},
      onConfirm: async filePath => { imported.push(filePath); },
    }, async () => {
      const upload = document.querySelector('[data-testid="connector-market-upload-modal"]');
      const confirm = upload.querySelector('[data-testid="connector-market-upload-confirm"]');
      const getWarning = () => document.querySelector('[data-testid="connector-market-confirm-dialog"]');
      await act(async () => upload.querySelector('[data-testid="connector-market-upload-dropzone"]').click());
      await act(async () => confirm.click());
      assert.deepEqual(inspected, [path]);

      if (packageId === null) {
        assert.equal(getWarning(), null);
        assert.deepEqual(catalogCalls, []);
      } else {
        assert.deepEqual(catalogCalls, [['mine', true]]);
        assert.deepEqual(imported, []);
        const warning = getWarning();
        assert.equal(warning.querySelector('h2').textContent, '开启多个系统插件');
        assert.equal(warning.querySelector('p').textContent, '同时开启多个系统插件可能导致功能冲突，是否确认开启此插件？');
        await act(async () => warning.querySelector('[data-testid="connector-market-confirm-cancel"]').click());
        assert.equal(getWarning(), null);
        assert.deepEqual(imported, []);

        await act(async () => confirm.click());
        const proceed = getWarning().querySelector('[data-testid="connector-market-confirm-ok"]');
        assert.equal(proceed.textContent, '确认开启');
        await act(async () => proceed.click());
      }
      assert.deepEqual(imported, [path]);
    });
  });
}

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

for (const [confirmation, version, outcome, fallback, expectedNavigation] of [
  ['secondary', '2.0.0', { ok: true, detail: 'removed' }, false, ['deleted']],
  ['ok', undefined, { ok: true, detail: 'removed' }, false, ['deleted']],
  ['secondary', '2.0.0', { ok: true, detail: 'removed' }, true, ['back']],
  ['secondary', '2.0.0', { ok: true, detail: 'updated' }, false, []],
  ['secondary', '2.0.0', { ok: true, detail: 'unavailable' }, false, []],
  ['secondary', '2.0.0', { ok: false, error: 'uninstall denied' }, false, []],
]) {
  test(`system plugin uninstall handles ${outcome.detail ?? 'failure'} (${confirmation}, fallback ${fallback})`, async () => {
    resetState();
    const state = globalThis.systemPluginActionState;
    state.detailCache['target-asset'] = {
      id: 'target-asset', assetId: 'target-asset', packageId: 'target-plugin', version: '2.0.0',
      versions: ['1.0.0', '2.0.0'], installedVersion: '2.0.0', displayName: { zh: '目标插件', en: 'Target plugin' },
      displayDescription: '', details: '', readme: '', tags: [], avatar: null, preview: null, events: [],
      installed: true, active: true, enabled: true, source: 'hub', origin: 'hub',
    };
    const detailCalls = [];
    state.loadDetail = async (...args) => { detailCalls.push(args); return true; };
    const uninstallCalls = [];
    state.uninstall = async (...args) => {
      uninstallCalls.push(args);
      if (outcome.detail === 'unavailable') state.detailCache = {};
      return outcome;
    };
    const navigation = [];
    await render(SystemPluginDetailPage, {
      id: 'target-asset', onBack: () => navigation.push('back'),
      onDeleted: fallback ? undefined : () => navigation.push('deleted'),
    }, async host => {
      await act(async () => host.querySelector('[data-testid="connector-market-system-plugin-detail-uninstall"]').click());
      const dialog = document.querySelector('[data-testid="connector-market-confirm-dialog"]');
      await act(async () => dialog.querySelector(`[data-testid="connector-market-confirm-${confirmation}"]`).click());
      assert.deepEqual(uninstallCalls, [['target-plugin', version, 'target-asset']]);
      assert.deepEqual(navigation, expectedNavigation);
      assert.deepEqual(detailCalls, [['target-asset']]);
      const error = host.querySelector('[data-testid="connector-market-system-plugin-detail-error"]');
      assert.equal(error?.textContent, outcome.error);
      if (outcome.detail === 'unavailable') {
        assert.ok(host.querySelector('[data-testid="connector-market-system-plugin-detail-state-body"] button'));
        assert.equal(host.querySelector('[data-testid="connector-market-system-plugin-detail-uninstall"]'), null);
      }
    });
  });
}

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

test('session creation preserves the selected memory plugin', async () => {
  const equipment = { plugin_names: ['agent-memory-plugin'] };
  const created = await createConversationSession(
    async () => ({ sessionId: 'web_real', projectId: 'default', workMode: 'work', session_equipment: equipment }),
    { create_token: 'stable-token', mode: 'agent', persist_session: true },
  );
  assert.deepEqual(created.session_equipment, equipment);
});

test('memory plugin detail shows the managed HTTP listener without transport controls', async () => {
  resetState({ installed: true, connected: true });
  globalThis.pluginActionState.detailCache[plugin.id].memoryAccess = {
    mode: 'http', managed_http: { running: true, base_url: 'http://127.0.0.1:8137' },
  };

  await render(PluginDetailPage, { id: plugin.id, onBack() {} }, async host => {
    assert.match(host.textContent, /内置 HTTP 服务/);
    assert.match(host.textContent, /服务运行中/);
    assert.match(host.textContent, /127\.0\.0\.1:8137/);
    assert.equal(host.querySelector('input[type="url"], input[type="password"], [role="radiogroup"]'), null);
  });
});

test('memory plugin detail remains usable without listener status', async () => {
  resetState({ installed: true, connected: true });
  globalThis.pluginActionState.detailCache[plugin.id].memoryAccess = { mode: 'http', managed_http: null };

  await render(PluginDetailPage, { id: plugin.id, fromMy: true, onBack() {}, onUse() {} }, async host => {
    const use = host.querySelector('[data-testid="connector-market-plugin-detail-use"]');
    assert.ok(use);
    assert.equal(use.disabled, false);
  });
});

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

test('system plugin detail uninstall confirms once when only one version exists', async () => {
  await i18next.changeLanguage('zh');
  const calls = resetState();
  const uninstalled = [];
  globalThis.systemPluginActionState.uninstall = async (id, version) => {
    uninstalled.push([id, version]);
    return { ok: true, detail: 'removed' };
  };
  globalThis.systemPluginActionState.detailCache['demo.suite'] = {
    id: 'demo.suite', packageId: 'demo.suite', version: '1.0.0', versions: ['1.0.0'],
    installedVersion: '1.0.0', displayName: { zh: '演示组合包', en: 'Demo suite' },
    displayDescription: { zh: '', en: 'Bundle' }, details: '', readme: '', tags: [],
    avatar: null, preview: null, events: [], installed: true, active: true, enabled: true,
    kind: 'bundle', overallStatus: 'enabled', systemPlugins: [], source: 'local', origin: 'user',
  };
  let deleted = 0;
  await render(
    SystemPluginDetailPage,
    { id: 'demo.suite', onBack() {}, onDeleted() { deleted += 1; } },
    async host => {
      await act(async () => host.querySelector('[data-testid="connector-market-system-plugin-detail-uninstall"]').click());
      const dialog = document.querySelector('[data-testid="connector-market-confirm-dialog"]');
      assert.ok(dialog);
      assert.equal(dialog.querySelector('[data-testid="connector-market-confirm-secondary"]'), null);
      assert.deepEqual(uninstalled, []);
      await act(async () => dialog.querySelector('[data-testid="connector-market-confirm-ok"]').click());
    },
  );
  assert.deepEqual(uninstalled, [['demo.suite', '1.0.0']]);
  assert.equal(deleted, 1);
});

test('system plugin detail toggle carries the version in view', async () => {
  await i18next.changeLanguage('zh');
  const calls = resetState();
  const toggles = [];
  globalThis.systemPluginActionState.setEnabled = async (id, enable, version) => {
    toggles.push([id, enable, version]);
    return { ok: true };
  };
  globalThis.systemPluginActionState.detailCache['demo.suite'] = {
    id: 'demo.suite', packageId: 'demo.suite', version: '1.0.0', versions: ['1.0.0', '1.0.1'],
    installedVersion: '1.0.0', displayName: { zh: '演示组合包', en: 'Demo suite' },
    displayDescription: { zh: '', en: 'Bundle' }, details: '', readme: '', tags: [],
    avatar: null, preview: null, events: [], installed: true, active: true, enabled: true,
    kind: 'bundle', overallStatus: 'enabled', systemPlugins: [], source: 'local', origin: 'user',
  };
  await render(SystemPluginDetailPage, { id: 'demo.suite', onBack() {} }, async host => {
    const toggle = host.querySelector('[data-testid="connector-market-system-plugin-detail-enabled"] button');
    assert.ok(toggle);
    await act(async () => toggle.click());
  });
  assert.deepEqual(toggles, [['demo.suite', false, '1.0.0']]);
});

for (const language of ['zh', 'en']) {
  test(`system plugin card shows the installing state while install runs (${language})`, async () => {
    await i18next.changeLanguage(language);
    const calls = resetState();
    const systemPlugin = {
      id: 'token-weather', packageId: 'token-weather', version: '1.0.0', installedVersion: '',
      displayName: { zh: 'Token 天气', en: 'Token Weather' },
      displayDescription: { zh: '系统插件', en: 'System plugin' },
      tags: [], avatar: null, installed: false, active: false, enabled: false,
    };
    globalThis.systemPluginActionState.catalog = [systemPlugin];
    globalThis.systemPluginActionState.installingIds = { 'token-weather': true };

    await render(MarketplacePage, marketProps(calls, 'plugin', 'plugin', 'system'), async host => {
      assert.match(host.textContent, language === 'zh' ? /安装中/ : /Installing/);
      assert.equal(host.querySelector('[data-testid="connector-market-card-install"]'), null);
    });
  });
}

test('system plugin card returns to the install action after install fails', async () => {
  await i18next.changeLanguage('zh');
  const calls = resetState();
  globalThis.systemPluginActionState.installingIds = {};
  globalThis.systemPluginActionState.catalog = [{
    id: 'token-weather', packageId: 'token-weather', version: '1.0.0', installedVersion: '',
    displayName: { zh: 'Token 天气', en: 'Token Weather' },
    displayDescription: { zh: '系统插件', en: 'System plugin' },
    tags: [], avatar: null, installed: false, active: false, enabled: false,
  }];

  await render(MarketplacePage, marketProps(calls, 'plugin', 'plugin', 'system'), async host => {
    assert.ok(host.querySelector('[data-testid="connector-market-card-install"]'));
    assert.doesNotMatch(host.textContent, /安装中/);
  });
});

test('system plugin detail install shows the installing label while running', async () => {
  await i18next.changeLanguage('zh');
  const calls = resetState();
  globalThis.systemPluginActionState.installingIds = { 'target-asset': true };
  globalThis.systemPluginActionState.detailCache['target-asset'] = {
    id: 'target-asset', assetId: 'target-asset', packageId: 'target-plugin', version: '1.0.0',
    versions: ['1.0.0'], installedVersion: '', displayName: { zh: '目标插件', en: 'Target plugin' },
    displayDescription: { zh: '待安装', en: 'Not installed' }, details: '', readme: '', tags: [],
    avatar: null, preview: null, events: [], installed: false, active: false, enabled: true,
    kind: 'plugin', overallStatus: 'not-installed', systemPlugins: [], source: 'hub', origin: 'hub',
  };

  await render(SystemPluginDetailPage, { id: 'target-asset', onBack() {} }, async host => {
    const button = host.querySelector('[data-testid="connector-market-system-plugin-detail-install"]');
    assert.ok(button);
    assert.match(button.textContent, /安装中/);
    assert.equal(button.getAttribute('aria-busy'), 'true');
    assert.equal(button.disabled, true);
  });
});

function themeDetail(versionStates) {
  return {
    id: 'theme.pkg', packageId: 'theme.pkg', version: '2.2.2',
    versions: ['2.0.0', '2.2.2'], installedVersion: '2.2.2',
    versionStates,
    displayName: { zh: '主题壁纸', en: 'Theme' },
    displayDescription: { zh: '', en: '' }, details: '', readme: '', tags: [],
    avatar: null, preview: null, events: [], installed: true, active: true, enabled: true,
    kind: 'plugin', overallStatus: 'enabled', source: 'hub', origin: 'hub',
  };
}

test('system plugin detail disables a version that failed to load', async () => {
  await i18next.changeLanguage('zh');
  resetState();
  globalThis.systemPluginActionState.detailCache['theme.pkg'] = themeDetail([
    { version: '2.0.0', status: 'failed', error: "No module named 'demo'" },
    { version: '2.2.2', status: 'active' },
  ]);

  await render(SystemPluginDetailPage, { id: 'theme.pkg', onBack() {} }, async host => {
    const trigger = host.querySelector('[data-testid="connector-market-system-plugin-detail-versions"]');
    assert.ok(trigger);
    assert.match(trigger.textContent, /2\.2\.2/);
    await act(async () => trigger.click());
    const failed = document.querySelector('[data-testid="ui-select-option"][data-variant="2.0.0"]');
    const active = document.querySelector('[data-testid="ui-select-option"][data-variant="2.2.2"]');
    assert.ok(failed);
    assert.equal(failed.disabled, true);
    assert.equal(failed.getAttribute('title'), '该版本加载失败，无法切换');
    assert.equal(active.disabled, false);
  });
});

test('system plugin detail returns to the installed version when a switch fails', async () => {
  await i18next.changeLanguage('zh');
  const attempts = [];
  const loads = [];
  resetState();
  globalThis.systemPluginActionState.install = async (id, version, options) => {
    attempts.push([id, version, options]);
    return { ok: false, error: "No module named 'demo'" };
  };
  globalThis.systemPluginActionState.loadDetail = async (id, version) => {
    loads.push([id, version]);
    return true;
  };
  globalThis.systemPluginActionState.detailCache['theme.pkg'] = themeDetail([
    { version: '2.0.0', status: 'standby' },
    { version: '2.2.2', status: 'active' },
  ]);

  await render(SystemPluginDetailPage, { id: 'theme.pkg', onBack() {} }, async host => {
    const trigger = host.querySelector('[data-testid="connector-market-system-plugin-detail-versions"]');
    await act(async () => trigger.click());
    const target = document.querySelector('[data-testid="ui-select-option"][data-variant="2.0.0"]');
    await act(async () => target.click());
    const dialog = document.querySelector('[data-testid="connector-market-confirm-dialog"]');
    assert.ok(dialog);
    await act(async () => dialog.querySelector('[data-testid="connector-market-confirm-ok"]').click());
    assert.equal(host.querySelector('[data-testid="connector-market-system-plugin-detail-error"]'), null);
    assert.equal(globalThis.connectorToastCalls.length, 1);
    const [notice] = globalThis.connectorToastCalls;
    assert.ok(notice);
    assert.equal(notice.variant, 'error');
    assert.match(notice.content, /2\.0\.0/);
    assert.match(notice.content, /2\.2\.2/);
    assert.match(notice.content, /原因/);
    assert.match(notice.content, /No module named/);
    assert.match(trigger.textContent, /2\.2\.2/);
  });
  assert.deepEqual(attempts, [['theme.pkg', '2.0.0', { reportError: false }]]);
  assert.deepEqual(loads.filter(([, version]) => version).at(-1), ['theme.pkg', '2.2.2']);
});

test('system plugin detail explains a busy package instead of showing the raw error', async () => {
  await i18next.changeLanguage('zh');
  resetState();
  globalThis.systemPluginActionState.detailCache['theme.pkg'] = themeDetail([
    { version: '2.2.2', status: 'active' },
  ]);
  globalThis.systemPluginActionState.setEnabled = async () => ({
    ok: false,
    error: "package has in-flight calls, package_id='theme.pkg'",
    code: 'EXTENSION_PACKAGE_BUSY',
  });

  await render(SystemPluginDetailPage, { id: 'theme.pkg', onBack() {} }, async host => {
    const toggle = host.querySelector('[data-testid="connector-market-system-plugin-detail-enabled"] button');
    assert.ok(toggle);
    await act(async () => toggle.click());
    const error = host.querySelector('[data-testid="connector-market-system-plugin-detail-error"]');
    assert.ok(error);
    assert.equal(error.textContent, '存在运行中任务使用当前插件，请任务结束或者强制终止后再操作。');
    assert.doesNotMatch(host.textContent, /in-flight/);
  });
});
