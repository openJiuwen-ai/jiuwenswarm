import assert from 'node:assert/strict';
import test, { after, before } from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';

const previousGlobals = new Map();
const client = {
  listeners: new Set(), requests: [], text: '', failOffset: null, paused: false,
  on(_event, callback) {
    this.listeners.add(callback);
    return () => this.listeners.delete(callback);
  },
  async request(_method, params, options) {
    this.requests.push({ params, options });
    if (this.paused) return;
    const offset = Number(params.cursor.split(':').at(-1));
    const prefix = { session_id: params.session_id, cursor: params.cursor };
    const bytes = Buffer.from(this.text);
    const end = Math.min(bytes.length, offset + 8192);
    const payload = this.failOffset === offset
      ? { ...prefix, status: 'error', code: 'HISTORY_ARTIFACT_FAILED' }
      : { ...prefix, message: {
        event_type: 'history.artifact', sha256: 'a'.repeat(64), bytes: bytes.length,
        offset, next_offset: end, content: bytes.subarray(offset, end).toString(), eof: end === bytes.length,
      } };
    queueMicrotask(() => { for (const callback of [...this.listeners]) callback({ payload }); });
  },
};
let dom;
let createRoot;
let ToolResultOriginal;
let I18nextProvider;
const i18n = i18next.createInstance();

before(async () => {
  dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true });
  for (const [name, value] of Object.entries({
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, Node: dom.window.Node,
    IS_REACT_ACT_ENVIRONMENT: true, __historyArtifactTestClient: client,
  })) {
    previousGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  ({ createRoot } = await import('react-dom/client'));
  const bundle = await build({
    stdin: {
      contents: 'export { ToolResultOriginal } from "./src/components/ChatPanel/ToolResultOriginal";',
      resolveDir: fileURLToPath(new URL('../', import.meta.url)), loader: 'ts',
    },
    bundle: true, write: false, platform: 'node', format: 'cjs', packages: 'external', jsx: 'automatic',
    loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
    define: { 'import.meta.env': '{}', 'import.meta.glob': 'testAssetGlob' },
    banner: { js: 'const testAssetGlob = () => ({});' },
    plugins: [{ name: 'artifact-test-client', setup(builder) {
      builder.onResolve({ filter: /services\/webClient$/ }, (args) => {
        if (args.importer.endsWith('ToolResultOriginal.tsx')) return { path: 'client', namespace: 'test' };
        return undefined;
      });
      builder.onLoad({ filter: /.*/, namespace: 'test' }, () => ({
        contents: 'export const webClient = globalThis.__historyArtifactTestClient;', loader: 'js',
      }));
    } }],
  });
  const compiled = { exports: {} };
  const require = createRequire(import.meta.url);
  new Function('require', 'module', 'exports', bundle.outputFiles[0].text)(require, compiled, compiled.exports);
  ({ ToolResultOriginal } = compiled.exports);
  ({ I18nextProvider } = require('react-i18next'));
  await i18n.init({ lng: 'en', resources: { en: { translation: { chatUi: { toolResult: {
    originalOpen: 'View original', originalClose: 'Close original', originalLoading: 'Loading',
    originalFailed: 'Failed', originalProgress: '{{percent}}%', originalNext: 'Next',
    originalPrevious: 'Previous', originalRetry: 'Retry',
  } } } } }, interpolation: { escapeValue: false } });
});

after(() => {
  dom?.window.close();
  for (const [name, descriptor] of previousGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

function result() {
  return { toolName: 'read_file', result: 'Preview only', success: true, artifactSessionId: 'current-session',
    payloadRefs: { rendered_result: { sha256: 'a'.repeat(64), bytes: Buffer.byteLength(client.text),
      media_type: 'text/plain' } } };
}

function element(name) {
  return document.querySelector(`[data-testid="chat-panel-tool-original-${name}"]`);
}

async function click(name) {
  const target = element(name);
  assert.ok(target && !target.disabled, `${name} must be enabled`);
  await act(async () => { target.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })); });
}

async function mount(value) {
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(createElement(I18nextProvider, { i18n },
    createElement(ToolResultOriginal, { result: value }))));
  return root;
}

test('original viewer loads on demand, keeps one window, retries the failed part and releases content', async () => {
  client.text = 'HEAD' + 'x'.repeat(140000) + 'TAIL';
  client.requests = [];
  const root = await mount(result());
  try {
    assert.equal(client.requests.length, 0);
    await click('toggle');
    assert.equal(client.requests.length, 8);
    assert.equal(Buffer.byteLength(element('content').textContent), 65536);
    assert.ok(element('content').textContent.startsWith('HEAD'));
    assert.equal(element('previous').disabled, true);
    client.failOffset = 65536;
    await click('next');
    assert.equal(element('status').textContent, 'Failed');
    assert.equal(element('content'), null);
    client.failOffset = null;
    await click('retry');
    assert.ok(client.requests.at(-8).params.cursor.endsWith(':65536'));
    assert.equal(Buffer.byteLength(element('content').textContent), 65536);
    await click('next');
    assert.ok(element('content').textContent.endsWith('TAIL'));
    assert.equal(element('next').disabled, true);
    await click('previous');
    assert.equal(Buffer.byteLength(element('content').textContent), 65536);
    await click('toggle');
    assert.equal(element('content'), null);
    assert.equal(client.listeners.size, 0);
    await click('toggle');
    assert.ok(element('content').textContent.startsWith('HEAD'));
  } finally {
    client.failOffset = null;
    await act(async () => root.unmount());
  }
});

test('closing an in-flight original read aborts it and removes the history subscriber', async () => {
  client.paused = true;
  client.requests = [];
  const root = await mount(result());
  try {
    await click('toggle');
    assert.equal(client.listeners.size, 1);
    const request = client.requests.at(-1);
    await click('toggle');
    assert.equal(request.options.signal.aborted, true);
    assert.equal(client.listeners.size, 0);
    assert.equal(element('viewer'), null);
  } finally {
    client.paused = false;
    await act(async () => root.unmount());
  }
});

test('ordinary small tool results expose no original reader or additional requests', async () => {
  client.requests = [];
  const root = await mount({ toolName: 'read_file', result: 'Complete small text', success: true });
  try {
    assert.equal(element('toggle'), null);
    assert.equal(client.requests.length, 0);
  } finally {
    await act(async () => root.unmount());
  }
});
