import assert from 'node:assert/strict';
import test, { before } from 'node:test';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import { build } from 'esbuild';
import { mkdir } from 'node:fs/promises';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { join } from 'node:path';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.defineProperty(globalThis, 'navigator', { value: dom.window.navigator, configurable: true });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const { createRoot } = await import('react-dom/client');
const root = createRoot(document.getElementById('root'));
const i18n = createInstance();
let McpDetailPage;
let connectorApi;

before(async () => {
  await i18n.init({ lng: 'zh-CN', fallbackLng: 'en', resources: { zh: { translation: {} }, en: { translation: {} } } });
  const project = fileURLToPath(new URL('..', import.meta.url));
  const cache = join(project, 'node_modules/.cache/mcp-detail-presentation');
  await mkdir(cache, { recursive: true });
  const stubs = {
    store: 'export const useConnectorStore = (selector) => selector(globalThis.__mcpDetailState);',
    web: 'export const webRequest = async () => ({item: globalThis.__mcpDetailRaw});',
    ui: `import React from 'react';
      export const DetailSection = ({children, title, titleTestId}) => React.createElement('section', null, React.createElement('h2', {'data-testid':titleTestId}, title), children);
      export const EntityHeader = ({title, actions}) => React.createElement('header', null, title, actions);
      export const PageCard = ({title, description, testId, variant}) => React.createElement('article', {'data-testid':testId, 'data-variant':variant}, title, React.createElement('p', null, description));
      export const DetailPromptChip = () => null;`,
    buttons: 'export const IconAvatar = () => null;',
    modals: 'export const ConnectTokenModal = () => null; export const CliAuthModal = () => null; export const ConfirmDialog = () => null;',
    svg: 'export default function Icon() { return null; }',
  };
  await build({
    absWorkingDir: project,
    entryPoints: { page: 'src/components/ConnectorMarket/McpDetailPage.tsx', api: 'src/services/connectorApi.ts' },
    outdir: cache,
    bundle: true,
    format: 'esm',
    platform: 'node',
    packages: 'external',
    jsx: 'automatic',
    define: { 'import.meta.env.DEV': 'false' },
    plugins: [{
      name: 'mcp-detail-boundaries',
      setup(builder) {
        const stub = (filter, path) => builder.onResolve({ filter }, () => ({ path, namespace: 'fixture' }));
        stub(/stores\/connectorStore$/, 'store');
        stub(/services\/webClient$|^\.\/webClient$/, 'web');
        stub(/^\.\.\/ui$/, 'ui');
        stub(/^\.\/Buttons$/, 'buttons');
        stub(/^\.\/(ConnectTokenModal|CliAuthModal|ConfirmDialog)$/, 'modals');
        stub(/\.svg\?react$/, 'svg');
        builder.onLoad({ filter: /.*/, namespace: 'fixture' }, ({ path }) => ({ contents: stubs[path], loader: 'js', resolveDir: project }));
      },
    }],
  });
  ({ McpDetailPage } = await import(pathToFileURL(join(cache, 'page.js')).href));
  ({ connectorApi } = await import(pathToFileURL(join(cache, 'api.js')).href));
});

async function render(raw, language) {
  globalThis.__mcpDetailRaw = raw;
  const detail = await connectorApi.show(raw.name);
  globalThis.__mcpDetailState = {
    connectors: [detail], detailCache: { [detail.runtimePackageName || detail.id]: detail }, busyMap: {},
    loadDetail: async () => detail,
  };
  await act(async () => {
    await i18n.changeLanguage(language);
    root.render(React.createElement(I18nextProvider, { i18n }, React.createElement(McpDetailPage, { name: detail.id, onBack() {} })));
  });
  return detail;
}

const query = (id) => document.querySelector(`[data-testid="${id}"]`);
const raw = () => ({
  name: 'gmail', display_name: 'Gmail', installed: true, source: 'built_in',
  integration_type: 'stdio-mcp', connection_state: 'connected', bundled_skills: ['gmail'],
  skills: [{ name: 'gmail', description: 'Bundled guide remains installed' }],
  tools: [{ name: 'search_emails', description: 'Original English description', description_i18n: { zh: '按条件搜索邮件', en: 'Search Gmail messages' } }],
});

test('Gmail hides only its skills section and switches localized tool text with the UI language', async () => {
  const detail = await render({ ...raw(), show_skills: false }, 'zh-CN');
  assert.equal(detail.skills.length, 1);
  assert.equal(query('connector-market-mcp-detail-skills'), null);
  assert.equal(query('connector-market-mcp-detail-tools').textContent.includes('按条件搜索邮件'), true);
  assert.equal(query('connector-market-mcp-detail-tool').getAttribute('data-variant'), 'search_emails');
  await render({ ...raw(), show_skills: false }, 'en');
  assert.equal(query('connector-market-mcp-detail-tools').textContent.includes('Search Gmail messages'), true);
});

test('legacy connectors retain skills and original descriptions when display metadata is absent', async () => {
  const legacy = { ...raw(), name: 'other', tools: [{ name: 'unknown_tool', description: 'Server fallback' }] };
  await render(legacy, 'zh-CN');
  assert.ok(query('connector-market-mcp-detail-skills'));
  assert.equal(query('connector-market-mcp-detail-tools').textContent.includes('Server fallback'), true);
});
