import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM, VirtualConsole } from 'jsdom';
import { createServer } from 'vite';
import svgr from 'vite-plugin-svgr';

const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => {
  if (error.type !== 'css parsing') throw error;
});
const dom = new JSDOM('<!doctype html><div id="root"></div>', {
  url: 'http://localhost/', pretendToBeVisual: true, virtualConsole,
});
const descriptors = new Map();
for (const [name, value] of Object.entries({
  window: dom.window, document: dom.window.document, localStorage: dom.window.localStorage,
  navigator: dom.window.navigator, Node: dom.window.Node, HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element, CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver, IS_REACT_ACT_ENVIRONMENT: true,
})) {
  descriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
}
const originalFetch = globalThis.fetch;
after(() => {
  globalThis.fetch = originalFetch;
  dom.window.close();
  for (const [name, descriptor] of descriptors) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

const vite = await createServer({
  configFile: false,
  plugins: [svgr(), {
    name: 'expose-production-file-card',
    enforce: 'pre',
    transform(code, id) {
      if (id.endsWith('/ChatPanel/MessageItem.tsx')) return code + '\nexport { FileDownloadList };';
    },
  }],
  resolve: { dedupe: ['react', 'react-dom', 'react-i18next', 'i18next', 'zustand'] },
  cacheDir: 'node_modules/.cache/file-download-card/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
  define: { 'import.meta.env.DEV': false },
});
let FileDownloadList;
try {
  ({ FileDownloadList } = await vite.ssrLoadModule('/src/components/ChatPanel/MessageItem.tsx'));
} finally { await vite.close(); }
const i18n = i18next.createInstance();
await i18n.init({
  lng: 'zh', initImmediate: false, showSupportNotice: false,
  resources: { zh: { translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8')) } },
});

async function withCard(edition, status, run) {
  window.__JIUWENSWARM_EDITION__ = edition;
  let currentStatus = status;
  let downloads = 0;
  const anchorClick = dom.window.HTMLAnchorElement.prototype.click;
  dom.window.HTMLAnchorElement.prototype.click = () => { downloads += 1; };
  globalThis.fetch = async () => {
    if (currentStatus === 'network') throw new TypeError('Failed to fetch');
    return new Response(null, { status: currentStatus });
  };
  const root = createRoot(document.getElementById('root'));
  const files = [{ name: '报告.docx', download_url: '/file-api/download?url=signed', size: 42 }];
  const render = () => act(async () => root.render(createElement(I18nextProvider, { i18n },
    createElement(FileDownloadList, { files: [...files] }))));
  const byId = id => document.querySelector(`[data-testid="${id}"]`);
  try {
    await render();
    await run({ byId, render, setStatus: value => { currentStatus = value; }, downloads: () => downloads });
  } finally {
    await act(async () => root.unmount());
    dom.window.HTMLAnchorElement.prototype.click = anchorClick;
    delete window.__JIUWENSWARM_EDITION__;
  }
}

for (const edition of ['enterprise', 'personal']) {
  for (const status of [200, 403, 404, 410, 405, 500, 503, 'network']) {
    test(`${edition} file card handles HEAD ${status}`, async () => {
      await withCard(edition, status, async ({ byId, setStatus, downloads }) => {
        const expired = status !== 200 && (edition === 'personal' || [403, 404, 410].includes(status));
        assert.equal(Boolean(byId('chat-panel-file-download-expired')), expired);
        const button = byId('chat-panel-file-download-btn');
        assert.equal(button.disabled, expired);
        setStatus(200);
        await act(async () => button.click());
        assert.equal(downloads(), expired ? 0 : 1);
      });
    });
  }
  test(`${edition} file card retains its expected recovery behavior`, async () => {
    await withCard(edition, 403, async ({ byId, setStatus, render }) => {
      assert.ok(byId('chat-panel-file-download-expired'));
      setStatus(200);
      await render();
      assert.equal(Boolean(byId('chat-panel-file-download-expired')), edition === 'personal');
    });
  });
}
