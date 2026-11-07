import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { JSDOM, VirtualConsole } from 'jsdom';
import { readFileSync } from 'node:fs';

// Install the browser before ReactDOM and the library inspect browser capabilities.
const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/', pretendToBeVisual: true, virtualConsole: new VirtualConsole(),
});
const iframeStyles = dom.window.document.createElement('style');
iframeStyles.textContent = readFileSync(new URL(
  '../src/features/webui/renderer/adapters/webuiIframe/Iframe.css', import.meta.url,
), 'utf8');
dom.window.document.head.append(iframeStyles);
const previous = new Map();
const channels = [];
const NativeMessageChannel = globalThis.MessageChannel;
class ComponentMessageChannel extends NativeMessageChannel {
  constructor() { super(); channels.push(this); }
}
const noop = () => {};
// jsdom does not implement the native modal-dialog API; browser tests cover modality.
dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', ''); };
dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open'); };
dom.window.matchMedia = () => ({
  matches: false, addListener: noop, removeListener: noop, addEventListener: noop, removeEventListener: noop,
});
class ResizeObserver { observe() {} unobserve() {} disconnect() {} }
for (const [name, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  Element: dom.window.Element, HTMLElement: dom.window.HTMLElement, SVGElement: dom.window.SVGElement,
  HTMLInputElement: dom.window.HTMLInputElement,
  ShadowRoot: dom.window.ShadowRoot, Node: dom.window.Node, MutationObserver: dom.window.MutationObserver,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window), ResizeObserver,
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true, MessageChannel: ComponentMessageChannel,
})) {
  previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
}
after(() => {
  for (const channel of channels) { channel.port1.close(); channel.port2.close(); }
  for (const [name, descriptor] of previous) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
  dom.window.close();
});

const { act, createElement, useEffect, useState } = await import('react');
const { createRoot } = await import('react-dom/client');
const { renderToStaticMarkup } = await import('react-dom/server');
const { theme } = await import('antd');
const {
  componentCatalog, componentRendererRegistry, createProtocolPipeline, WebUISchemaRenderer, FileTree, createComponentState,
  createWebUIRuntime, createWebUIAppRuntime, createMockTransport, WebUIHost, WebUIEntryHost, WebUIProvider, WebUIThemeProvider,
  WebUISlotProvider, WebUIContentActivity, resolveWebUITheme, i18n, buttonIcon, browserClientCapabilities, GatewayImage,
  usePluginSettings,
  SettingsServicesProvider,
  panelNavigation,
  requestPanelOpen,
  useWebUIPanels,
  WebUINavigationHost,
  WebUIPageHost,
  WebUIBackgroundHost,
  WebUIAppearanceHost, appearanceModule, splashModule, WebUISplashGate,
} = await import('../node_modules/.cache/webui-components/components.mjs');

const target = {
  session_id: 'sess-1', slot_id: 'conversation.input.above', package_id: 'pkg.demo', instance_key: 'main',
};

test('protected images mount only after resource loading and discard old-context URLs before loading again', async () => {
  const host = browserClientCapabilities;
  const originalFetch = globalThis.fetch;
  const desktop = window.jiuwenDesktop;
  const createObjectURL = URL.createObjectURL;
  const revokeObjectURL = URL.revokeObjectURL;
  const storageKeys = new Set(Object.keys(window.localStorage));
  const pending = [];
  const revoked = [];
  let serial = 0;
  window.jiuwenDesktop = { apiBase: 'http://localhost' };
  globalThis.fetch = (url) => new Promise((resolve) => pending.push({ url, resolve }));
  URL.createObjectURL = () => `blob:http://localhost/resource-${++serial}`;
  URL.revokeObjectURL = (url) => revoked.push(url);
  host.configure({ clientProtocolVersion: 1, gatewayInstanceId: 'image-gateway', connectionEpoch: 'image-connection',
    packages: [{ packageId: 'image-package', activationGeneration: 'image-generation',
      requestBindings: [{ type: 'http',
        source: { key: 'credential' }, target: { location: 'header', name: 'Authorization', prefix: 'Bearer ' } }] }] });
  await Promise.resolve();
  const app = await setup({ component: 'Image', props: { src: '/protected-image', alt: 'Protected', preview: false } });
  const container = document.createElement('div'); document.body.append(container);
  const raw = createRoot(container);
  const completeImages = async () => {
    const batch = pending.splice(0);
    assert.equal(batch.length, 2);
    await act(async () => {
      for (const request of batch) request.resolve(new Response(new Uint8Array([137, 80, 78, 71]),
        { headers: { 'Content-Type': 'image/png' } }));
    });
  };
  try {
    await act(async () => raw.render(createElement(GatewayImage, { src: '/protected-native-image', alt: 'Native' })));
    assert.equal(app.container.querySelector('img'), null);
    assert.equal(container.querySelector('img'), null);
    await completeImages();
    const first = [app.container.querySelector('img').src, container.querySelector('img').src];
    assert.ok(first.every((url) => url.startsWith('blob:')));
    await act(async () => host.acceptOperation({ clientProtocolVersion: 1,
      invocation_id: 'image-invocation', connection_epoch: 'image-connection', package_id: 'image-package',
      activation_generation: 'image-generation', method: 'local_storage.set',
      params: { key: 'credential', value: 'changed' }, deadline: Date.now() + 30_000 }));
    assert.equal(app.container.querySelector('img'), null);
    assert.equal(container.querySelector('img'), null);
    assert.ok(first.every((url) => revoked.includes(url)));
    await completeImages();
    assert.ok([app.container.querySelector('img').src, container.querySelector('img').src]
      .every((url) => url.startsWith('blob:') && !first.includes(url)));
  } finally {
    await app.close();
    await act(async () => raw.unmount()); container.remove();
    host.invalidate();
    for (const key of Object.keys(window.localStorage)) if (!storageKeys.has(key)) window.localStorage.removeItem(key);
    window.jiuwenDesktop = desktop;
    globalThis.fetch = originalFetch;
    URL.createObjectURL = createObjectURL;
    URL.revokeObjectURL = revokeObjectURL;
  }
});

for (const loaded of [true, false]) {
  test(`package background and images survive credential changes with resources ${loaded ? 'loaded' : 'pending'}`, async () => {
    const host = browserClientCapabilities;
    const originalFetch = globalThis.fetch;
    const desktop = window.jiuwenDesktop;
    const createObjectURL = URL.createObjectURL;
    const revokeObjectURL = URL.revokeObjectURL;
    const storageKeys = new Set(Object.keys(window.localStorage));
    const pending = [];
    const revoked = [];
    let serial = 0;
    let ready;
    window.jiuwenDesktop = { apiBase: 'http://localhost' };
    globalThis.fetch = (url, init) => new Promise((resolve) => pending.push({ url, init, resolve }));
    URL.createObjectURL = () => `blob:http://localhost/package-resource-${++serial}`;
    URL.revokeObjectURL = (url) => revoked.push(url);
    const snapshot = { clientProtocolVersion: 1, gatewayInstanceId: 'asset-gateway', connectionEpoch: 'asset-connection',
      packages: [{ packageId: 'asset-package', activationGeneration: 'asset-generation',
        requestBindings: [{ type: 'http',
          source: { key: 'credential' }, target: { location: 'header', name: 'Authorization', prefix: 'Bearer ' } }] }] };
    host.configure(snapshot);
    await Promise.resolve();
    const prefix = '/extension-assets/asset-package/asset-generation/';
    const app = await setup({ component: 'Box', props: { backgroundImage: prefix + 'background.svg' }, children: [
      { component: 'Image', props: { src: prefix + 'promotion.png', alt: 'Promotion', preview: false } },
      { component: 'Image', props: { src: '/protected-account-image', alt: 'Account', preview: false } },
      { component: 'Input', props: { placeholder: 'Username' } },
    ] }, { ...target, package_id: 'asset-package' }, {}, { activation_generation: 'asset-generation' });
    const container = document.createElement('div'); document.body.append(container);
    const raw = createRoot(container);
    const complete = async (requests) => {
      await act(async () => {
        for (const request of requests) request.resolve(new Response(new Uint8Array([137, 80, 78, 71]),
          { headers: { 'Content-Type': 'image/png' } }));
      });
    };
    try {
      await act(async () => raw.render(createElement(GatewayImage, { src: prefix + 'native.png', alt: 'Native' })));
      const initial = pending.splice(0);
      assert.equal(initial.length, 4);
      if (loaded) await complete(initial);
      const box = app.container.querySelector('[data-testid="webui-box"]');
      const form = app.container.querySelector('input');
      const promotion = app.container.querySelector('img[alt="Promotion"]');
      const native = container.querySelector('img');
      const background = box.style.backgroundImage;
      const accountUrl = app.container.querySelector('img[alt="Account"]')?.src;
      host.setInvocationWaiter(() => new Promise((resolve) => { ready = resolve; }));
      await act(async () => host.acceptOperation({ clientProtocolVersion: 1,
        invocation_id: 'asset-invocation', connection_epoch: 'asset-connection', package_id: 'asset-package',
        activation_generation: 'asset-generation', method: 'local_storage.set',
        params: { key: 'credential', value: 'changed' }, deadline: Date.now() + 30_000 }));
      assert.equal(host.isChanging(), true);
      assert.equal(app.container.querySelector('input'), form);
      assert.equal(app.container.querySelector('img[alt="Account"]'), null);
      if (loaded) {
        assert.equal(app.container.querySelector('img[alt="Promotion"]'), promotion);
        assert.equal(container.querySelector('img'), native);
        assert.equal(box.style.backgroundImage, background);
        assert.deepEqual(revoked, [accountUrl]);
      } else {
        assert.ok(initial.filter((item) => item.url.startsWith(prefix)).every((item) => !item.init.signal.aborted));
        assert.equal(initial.find((item) => item.url === '/protected-account-image').init.signal.aborted, true);
        await complete(initial);
      }
      const urls = [app.container.querySelector('img[alt="Promotion"]').src, container.querySelector('img').src];
      assert.ok(urls.every((url) => url.startsWith('blob:')));
      assert.match(box.style.backgroundImage, /blob:/);
      assert.equal(pending.length, 0, 'credential changes do not reload package assets');
      await act(async () => ready());
      assert.equal(pending.length, 1);
      assert.equal(pending[0].url, '/protected-account-image');
      assert.equal(pending[0].init.headers.get('Authorization'), 'Bearer changed');
      await complete(pending.splice(0));
      assert.ok(app.container.querySelector('img[alt="Account"]'));
      const next = structuredClone(snapshot);
      if (loaded) next.packages[0].activationGeneration = 'reactivated';
      else next.packages = [];
      await act(async () => host.configure(next));
      assert.equal(app.container.querySelector('img[alt="Promotion"]'), null);
      assert.equal(container.querySelector('img').src, urls[1], 'standalone resource follows its own mount lifetime');
      assert.equal(box.isConnected, false, 'the inactive Gateway plugin view is unmounted');
      assert.ok(revoked.includes(urls[0]));
      assert.ok(pending.every((item) => !item.url.startsWith(prefix)), 'old activations are never requested again');
    } finally {
      ready?.();
      await app.close();
      await act(async () => raw.unmount()); container.remove();
      host.setInvocationWaiter(() => Promise.resolve());
      host.invalidate();
      for (const key of Object.keys(window.localStorage)) if (!storageKeys.has(key)) window.localStorage.removeItem(key);
      window.jiuwenDesktop = desktop;
      globalThis.fetch = originalFetch;
      URL.createObjectURL = createObjectURL;
      URL.revokeObjectURL = revokeObjectURL;
    }
  });
}

test('FilePreview embeds images and verified PDFs using the native browser viewer', async () => {
  const originalFetch = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options });
    return new Response('%PDF-', { status: 206, headers: { 'Content-Type': 'application/pdf' } });
  };
  const fileTree = (name, mimeType, downloadUrl) => ({
    component: 'Panel', props: { title: 'Files' }, children: [{
      component: 'FilePreview', props: { file: { name, path: name, mimeType, downloadUrl } },
    }],
  });
  const app = await setup(fileTree('pic.png', 'image/png', '/file-api/download?token=image#page'),
    { ...target, slot_id: 'conversation.panel.right' });
  try {
    const image = app.container.querySelector('[data-testid="webui-file-preview-image"]');
    assert.ok(image);
    assert.equal(image.alt, 'pic.png');
    const imageUrl = new URL(image.src);
    assert.equal(imageUrl.searchParams.get('token'), 'image');
    assert.equal(imageUrl.searchParams.get('inline'), '1');
    assert.equal(imageUrl.hash, '#page');
    await act(async () => image.dispatchEvent(new window.Event('error')));
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-state"]').dataset.variant, 'error');

    assert.equal(requests.length, 0, 'the existing image path does not fetch PDF metadata');
    await app.replace(fileTree('report.pdf', 'application/pdf', '/file-api/download?token=pdf&user_id=user-a#page=2'));
    const pdf = app.container.querySelector('[data-testid="webui-file-preview-pdf"]');
    assert.ok(pdf);
    assert.equal(pdf.title, 'report.pdf');
    assert.equal(pdf.hasAttribute('sandbox'), false);
    assert.equal(pdf.getAttribute('referrerpolicy'), 'no-referrer');
    const pdfUrl = new URL(pdf.src);
    assert.equal(pdfUrl.origin, window.location.origin);
    assert.equal(pdfUrl.pathname, '/file-api/download');
    assert.equal(pdfUrl.searchParams.get('token'), 'pdf');
    assert.equal(pdfUrl.searchParams.get('user_id'), 'user-a');
    assert.equal(pdfUrl.searchParams.get('inline'), '1');
    assert.equal(pdfUrl.hash, '#page=2');
    assert.equal(requests.length, 1);
    assert.equal(requests[0].options.headers.Range, 'bytes=0-4');
    assert.equal(requests[0].options.redirect, 'error');
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-title"]').textContent, 'report.pdf');

    await app.replace({ component: 'Panel', props: { title: 'Files' }, children: [{
      component: 'FilePreview', props: { status: 'forbidden', file: null },
    }] }, 3);
    assert.equal(app.container.querySelector('iframe, img'), null);
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-state"]').dataset.variant, 'forbidden');
  } finally { await app.close(); globalThis.fetch = originalFetch; }
});

test('FilePreview rejects arbitrary PDF sources and non-PDF download responses before iframe navigation', async () => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async () => { requests += 1; throw new Error('must not fetch arbitrary URLs'); };
  const app = await setup({ component: 'FilePreview', props: { file: null } });
  const tree = (downloadUrl) => ({ component: 'FilePreview', props: { file: {
    name: 'report.pdf', path: 'report.pdf', mimeType: 'application/pdf', downloadUrl,
  } } });
  try {
    let revision = 1;
    for (const url of ['https://example.com/report.pdf', '/page.html?token=test', '/file-api/download']) {
      await app.replace(tree(url), ++revision);
      assert.equal(app.container.querySelector('iframe'), null);
      assert.equal(app.container.querySelector('[data-testid="webui-file-preview-state"]').dataset.variant, 'forbidden');
    }
    assert.equal(requests, 0);
    for (const [response, expected] of [
      [new Response('<html>page</html>', { headers: { 'Content-Type': 'text/html' } }), 'unsupported'],
      [new Response('%PDF-', { headers: { 'Content-Type': 'text/html' } }), 'unsupported'],
      [new Response('<html>', { headers: { 'Content-Type': 'application/pdf' } }), 'unsupported'],
      [new Response('%PDF', { headers: { 'Content-Type': 'application/pdf' } }), 'unsupported'],
      [new Response('missing', { status: 404 }), 'missing'],
      [new Response('denied', { status: 403 }), 'forbidden'],
      [new Response('failed', { status: 500 }), 'error'],
    ]) {
      globalThis.fetch = async () => response;
      await app.replace(tree(`/file-api/download?token=${++revision}`), revision);
      assert.equal(app.container.querySelector('iframe'), null);
      assert.equal(app.container.querySelector('[data-testid="webui-file-preview-state"]').dataset.variant, expected);
    }
  } finally { await app.close(); globalThis.fetch = originalFetch; }
});

test('FilePreview bounds the PDF probe and cancels a late probe when the selection changes', async () => {
  const originalFetch = globalThis.fetch;
  let cancelled = false;
  let reads = 0;
  globalThis.fetch = async () => new Response(new ReadableStream({
    pull(controller) {
      reads += 1;
      controller.enqueue(new TextEncoder().encode(reads === 1 ? '%P' : 'DF-more PDF bytes'));
    },
    cancel() { cancelled = true; },
  }), { headers: { 'Content-Type': 'application/pdf; charset=binary' } });
  const tree = (token) => ({ component: 'FilePreview', props: { file: {
    name: 'report.pdf', path: 'report.pdf', mimeType: 'application/pdf', downloadUrl: `/file-api/download?token=${token}`,
  } } });
  const app = await setup(tree('first'));
  try {
    assert.ok(app.container.querySelector('iframe'));
    assert.ok(cancelled, 'stop the probe even when the server returns a full 200 response');
    assert.ok(reads <= 3, 'consume only the chunks needed for the PDF signature');
    let complete;
    let signal;
    globalThis.fetch = (_url, options) => {
      signal = options.signal;
      return new Promise((resolve) => { complete = resolve; });
    };
    await app.replace(tree('pending'));
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-state"]').dataset.variant, 'loading');
    await app.replace({ component: 'FilePreview', props: { file: {
      name: 'text.txt', path: 'text.txt', content: 'current selection',
    } } }, 3);
    assert.equal(signal.aborted, true);
    await act(async () => complete(new Response('%PDF-', { headers: { 'Content-Type': 'application/pdf' } })));
    assert.equal(app.container.querySelector('iframe'), null);
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-text"]').textContent, 'current selection');
  } finally { await app.close(); globalThis.fetch = originalFetch; }
});

test('FilePreview loads larger UTF-8 text while preserving inline text and literal HTML', async () => {
  const originalFetch = globalThis.fetch;
  const text = '较大文本\n'.repeat(5000);
  let requests = 0;
  globalThis.fetch = async () => { requests += 1; return new Response('\uFEFF' + text); };
  const app = await setup({ component: 'FilePreview', props: { file: {
    name: 'large.txt', path: 'large.txt', mimeType: 'text/plain', size: new TextEncoder().encode(text).length,
    downloadUrl: '/file-api/download?token=text',
  } } });
  try {
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-text"]').textContent, text);
    assert.equal(requests, 1);
    await app.replace({ component: 'FilePreview', props: { file: {
      name: 'large.txt', path: 'large.txt', mimeType: 'text/plain', content: 'inline replacement',
      downloadUrl: '/file-api/download?token=text',
    } } });
    assert.equal(requests, 1);
    let complete;
    globalThis.fetch = async () => {
      requests += 1;
      return new Promise((resolve) => { complete = resolve; });
    };
    await app.replace({ component: 'FilePreview', props: { file: {
      name: 'large.txt', path: 'large.txt', mimeType: 'text/plain', downloadUrl: '/file-api/download?token=text',
    } } }, 3);
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-state"]').dataset.variant, 'loading');
    await act(async () => complete(new Response('refreshed text')));
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-text"]').textContent, 'refreshed text');
    await app.replace({ component: 'FilePreview', props: { file: {
      name: 'page.html', path: 'page.html', content: '<script>alert(1)</script>',
      downloadUrl: '/file-api/download?token=unused',
    } } }, 4);
    assert.equal(requests, 2);
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-text"]').textContent, '<script>alert(1)</script>');
    assert.equal(app.container.querySelector('script, iframe'), null);
  } finally { await app.close(); globalThis.fetch = originalFetch; }
});

test('FilePreview bounds fetched text and maps failed requests to existing notices', async () => {
  const originalFetch = globalThis.fetch;
  const app = await setup({ component: 'FilePreview', props: { file: null } });
  try {
    let revision = 1;
    for (const [response, expected] of [
      [new Response('missing', { status: 404 }), 'missing'],
      [new Response('denied', { status: 403 }), 'forbidden'],
      [new Response('failed', { status: 500 }), 'error'],
      [new Response(new Uint8Array(1024 * 1024 + 1)), 'too_large'],
      [new Response(new Uint8Array([255])), 'unsupported'],
      [new Response('binary\0data'), 'unsupported'],
    ]) {
      globalThis.fetch = async () => response;
      revision += 1;
      await app.replace({ component: 'FilePreview', props: { file: {
        name: 'large.txt', path: 'large.txt', mimeType: 'text/plain',
        downloadUrl: `/file-api/download?token=${revision}`,
      } } }, revision);
      assert.equal(app.container.querySelector('[data-testid="webui-file-preview-state"]').dataset.variant, expected);
    }
  } finally { await app.close(); globalThis.fetch = originalFetch; }
});

test('FilePreview aborts pending text on selection changes and ignores a late response', async () => {
  const originalFetch = globalThis.fetch;
  let complete;
  let signal;
  globalThis.fetch = (_url, options) => {
    signal = options.signal;
    return new Promise((resolve) => { complete = resolve; });
  };
  const app = await setup({ component: 'FilePreview', props: { file: {
    name: 'slow.txt', path: 'slow.txt', mimeType: 'text/plain', downloadUrl: '/file-api/download?token=slow',
  } } });
  try {
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-state"]').dataset.variant, 'loading');
    await app.replace({ component: 'FilePreview', props: { file: {
      name: 'current.txt', path: 'current.txt', content: 'current selection',
    } } });
    assert.equal(signal.aborted, true);
    await act(async () => complete(new Response('stale selection')));
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-text"]').textContent, 'current selection');
  } finally { await app.close(); globalThis.fetch = originalFetch; }
});

const frame = (root, revision = 1, scope = target) => ({
  target: scope, schema_version: 1, revision, mode: 'replace', root,
});
const events = { 'ui.click': { id: 'confirm', args: { source: 'toolbar' } } };
async function click(element) {
  assert.ok(element, 'click target must exist');
  await act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true })));
}
async function changeInput(input, value, isComposing = false) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new window.InputEvent('input', { bubbles: true, inputType: 'insertText', isComposing }));
  });
}
function untilDOM(container, predicate) {
  if (predicate()) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const observer = new MutationObserver(() => { if (predicate()) { observer.disconnect(); clearTimeout(deadline); resolve(); } });
    const deadline = setTimeout(() => { observer.disconnect(); reject(new Error('Expected DOM update did not arrive')); }, 2500);
    observer.observe(container, { subtree: true, childList: true, attributes: true });
  });
}
async function pressEnter(input, options = {}) {
  await act(async () => {
    const init = { key: 'Enter', keyCode: 13, bubbles: true, ...options };
    input.dispatchEvent(new window.KeyboardEvent('keydown', init));
    input.dispatchEvent(new window.KeyboardEvent('keyup', init));
  });
}
async function pressKey(element, name, code) {
  const init = { key: name, code: name, keyCode: code, which: code, bubbles: true };
  await act(async () => element.dispatchEvent(new window.KeyboardEvent('keydown', init)));
  await act(async () => element.dispatchEvent(new window.KeyboardEvent('keyup', init)));
}
async function setup(tree, scope = target, transportOptions = {}, frameExtra = {}) {
  const transport = createMockTransport(transportOptions);
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  let active = true;
  const render = () => root.render(createElement(WebUIProvider, { runtime, registry: componentRendererRegistry },
    createElement(WebUIContentActivity, { active },
      createElement(WebUIHost, { sessionId: scope.session_id, slotId: scope.slot_id }))));
  await act(async () => render());
  await act(async () => transport.injectRenderFrame({ ...frame(tree, 1, scope), ...frameExtra }));
  return {
    container, runtime, transport,
    business: () => transport.dispatches.filter((request) => request.event !== 'ui.ready'),
    async replace(next, revision = 2) {
      await act(async () => transport.injectRenderFrame({ ...frame(next, revision, scope), ...frameExtra }));
    },
    async activity(value) { active = value; await act(async () => render()); },
    async close() { await act(async () => root.unmount()); runtime.dispose(); container.remove(); },
  };
}

test('Button opens only its own live panel after an accepted receipt; renders never navigate', async () => {
  const app = await setup(
    {
      component: 'Button',
      props: { value: 'Details', openPanel: 'details' },
      events: { 'ui.click': { id: 'details', args: {} } },
    },
    target,
    { autoAccept: false },
  );
  const owner = app.runtime.mountSlot(target.session_id, 'conversation.panel.right', { notifyReady: false });
  const destination = { ...target, slot_id: 'conversation.panel.right', instance_key: 'details' };
  const tree = { component: 'Iframe', props: { title: 'Details', srcDoc: '<p>Usage</p>' } };
  const navigation = panelNavigation(app.runtime);
  try {
    await act(async () => app.transport.resolveNextDispatch({ accepted: true, status: 'ready' }));
    await act(async () => app.transport.injectRenderFrame(frame(tree, 1, destination)));
    assert.equal(navigation.getSnapshot(), null);
    await click(app.container.querySelector('button'));
    assert.equal(navigation.getSnapshot(), null, 'no navigation while the action is unresolved');
    await act(async () => app.transport.resolveNextDispatch({ accepted: false, status: 'rejected' }));
    assert.equal(navigation.getSnapshot(), null);
    await click(app.container.querySelector('button'));
    await act(async () => app.transport.resolveNextDispatch({ accepted: true, status: 'delivered' }));
    const request = navigation.getSnapshot();
    assert.equal(request.sessionId, target.session_id);
    assert.equal(request.key, 'sess-1|conversation.panel.right|pkg.demo|details');
    navigation.consume(request.id);
    await act(async () => app.transport.injectRenderFrame(frame(tree, 2, destination)));
    assert.equal(navigation.getSnapshot(), null, 'data refreshes do not reopen or select panels');
    const source = {
      sessionId: target.session_id,
      slotId: target.slot_id,
      packageId: target.package_id,
      instanceKey: target.instance_key,
    };
    await act(async () =>
      app.transport.injectRenderFrame(frame(tree, 1, { ...destination, package_id: 'other', instance_key: 'foreign' })),
    );
    assert.equal(requestPanelOpen(app.runtime, source, 'foreign'), false);
    assert.equal(requestPanelOpen(app.runtime, { ...source, sessionId: 'another-session' }, 'details'), false);
    assert.equal(navigation.getSnapshot(), null);
  } finally {
    owner.release();
    await app.close();
  }
});

test('page-owned panel discovery survives a hidden body and opens only on explicit navigation', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  const destination = { ...target, slot_id: 'conversation.panel.right', instance_key: 'details' };
  const details = (value) => ({ component: 'Panel', props: { title: 'Details' },
    children: [{ component: 'Text', props: { value } }] });
  function Page() {
    const { panels, renderPanel, openRequest, consumeOpenRequest } = useWebUIPanels({
      sessionId: target.session_id, slotId: destination.slot_id,
    });
    const [selected, setSelected] = useState(null);
    useEffect(() => {
      if (!openRequest || !panels.some((panel) => panel.key === openRequest.key)) return;
      setSelected(openRequest.key);
      consumeOpenRequest(openRequest.id);
    }, [openRequest, panels, consumeOpenRequest]);
    return createElement('div', null,
      createElement(WebUIHost, { sessionId: target.session_id, slotId: target.slot_id }),
      selected ? createElement('section', null, renderPanel(selected),
        createElement('button', { onClick: () => setSelected(null) }, 'Hide details')) : null);
  }
  try {
    await act(async () => root.render(createElement(WebUIProvider,
      { runtime, registry: componentRendererRegistry }, createElement(Page))));
    await act(async () => {
      transport.injectRenderFrame(frame({ component: 'Button', props: { value: 'Open details', openPanel: 'details' },
        events: { 'ui.click': { id: 'details' } } }, 1));
      transport.injectRenderFrame(frame(details('First reading'), 1, destination));
    });
    assert.equal(container.querySelector('section'), null, 'background frames do not display the panel');
    await click(container.querySelector('[data-testid="webui-button"]'));
    assert.match(container.querySelector('section').textContent, /First reading/);
    await click([...container.querySelectorAll('button')].find((button) => button.textContent === 'Hide details'));
    assert.equal(container.querySelector('section'), null);
    assert.equal(runtime.getScopeSnapshot(target.session_id, destination.slot_id).mounted, true);
    await act(async () => transport.injectRenderFrame(frame(details('Second reading'), 2, destination)));
    assert.equal(container.querySelector('section'), null, 'updates do not reopen a hidden body');
    await click(container.querySelector('[data-testid="webui-button"]'));
    assert.match(container.querySelector('section').textContent, /Second reading/);
  } finally {
    await act(async () => root.unmount()); runtime.dispose(); container.remove();
  }
});

test('Iframe selects complete localized documents and keeps the fixed sandbox without a URL source', async () => {
  const previousLanguage = i18n.language;
  await act(async () => i18n.changeLanguage('en'));
  const documents = { zh: '<!doctype html><p>用量</p>', en: '<!doctype html><p>Usage</p>' };
  const app = await setup(
    {
      component: 'Iframe',
      props: {
        title: { zh: '用量详情', en: 'Usage details' },
        srcDoc: documents,
      },
    },
    { ...target, slot_id: 'conversation.panel.right' },
  );
  try {
    const first = app.container.querySelector('iframe');
    assert.equal(first.title, 'Usage details');
    assert.equal(first.getAttribute('srcdoc'), documents.en);
    assert.equal(first.hasAttribute('src'), false);
    assert.equal(app.container.querySelector('[data-testid="webui-iframe-open-source"]'), null);
    assert.equal(app.container.querySelector('[data-testid="webui-iframe-source"]').textContent, 'Plugin page');
    assert.equal(first.getAttribute('sandbox'), 'allow-scripts allow-forms allow-popups allow-modals allow-downloads');
    await act(async () => i18n.changeLanguage('zh'));
    const translated = app.container.querySelector('iframe');
    assert.equal(translated.title, '用量详情');
    assert.equal(translated.getAttribute('srcdoc'), documents.zh);
    assert.equal(app.container.querySelector('[data-testid="webui-iframe-source"]').textContent, '插件页面');
    assert.notEqual(first, translated);
    await app.activity(false);
    assert.equal(app.container.querySelector('iframe'), null);
    await app.activity(true);
    assert.equal(app.container.querySelector('iframe').getAttribute('srcdoc'), documents.zh);
  } finally {
    await app.close();
    await act(async () => i18n.changeLanguage(previousLanguage));
  }
});

test('Iframe selects localized file URLs, releases the old document and retains its sandbox', async () => {
  const previousLanguage = i18n.language;
  await act(async () => i18n.changeLanguage('en'));
  const urls = { zh: '/extension-assets/_agent/route/set/zh.html', en: '/extension-assets/_agent/route/set/en.html' };
  const app = await setup({
    component: 'Iframe', props: { title: { zh: '详情', en: 'Details' }, src: urls },
  }, { ...target, slot_id: 'conversation.panel.right' });
  try {
    const first = app.container.querySelector('iframe');
    assert.equal(first.getAttribute('src'), urls.en);
    assert.equal(first.hasAttribute('srcdoc'), false);
    await act(async () => i18n.changeLanguage('zh'));
    const translated = app.container.querySelector('iframe');
    assert.notEqual(first, translated);
    assert.equal(translated.getAttribute('src'), urls.zh);
    assert.equal(translated.title, '详情');
    assert.equal(translated.getAttribute('sandbox'), 'allow-scripts allow-forms allow-popups allow-modals allow-downloads');
    await app.activity(false);
    assert.equal(app.container.querySelector('iframe'), null);
    await app.activity(true);
    assert.equal(app.container.querySelector('iframe').getAttribute('src'), urls.zh);
  } finally {
    await app.close();
    await act(async () => i18n.changeLanguage(previousLanguage));
  }
});

for (const { pageUrl, apiBase, expectedBase } of [
  {
    pageUrl: 'file:///Applications/Jiuwen/dist/index.html',
    apiBase: 'http://127.0.0.1:19000',
    expectedBase: 'http://127.0.0.1:19000',
  },
  {
    pageUrl: 'file:///C:/Jiuwen/dist/index.html',
    apiBase: 'http://127.0.0.1:19000',
    expectedBase: 'http://127.0.0.1:19000',
  },
  {
    pageUrl: 'https://web.example/chat/new',
    apiBase: 'https://gateway.example/agent/',
    expectedBase: 'https://gateway.example/agent',
  },
]) {
  test(`Iframe resolves plugin resources through the configured Gateway from ${pageUrl}`, async () => {
    const previousUrl = window.location.href;
    const desktop = window.jiuwenDesktop;
    const resourcePath = '/extension-assets/_agent/route/set/en.html';
    let app;
    dom.reconfigure({ url: pageUrl });
    assert.equal(window.location.href, pageUrl);
    window.jiuwenDesktop = { apiBase };
    try {
      app = await setup({ component: 'Iframe', props: { title: 'Details', src: resourcePath } },
        { ...target, slot_id: 'conversation.panel.right' });
      const iframe = app.container.querySelector('iframe');
      const expected = expectedBase + resourcePath;
      assert.equal(iframe.src, expected);
      assert.equal(app.container.querySelector('[data-testid="webui-iframe-open-source"]').href, expected);
      assert.equal(app.container.querySelector('[data-testid="webui-iframe-source"]').textContent, expected);
      assert.equal(new URL('details.css', iframe.src).href, expectedBase + resourcePath.replace('en.html', 'details.css'));
      assert.equal(iframe.getAttribute('sandbox'), 'allow-scripts allow-forms allow-popups allow-modals allow-downloads');
      await app.replace({ component: 'Iframe', props: { title: 'External', src: 'https://example.com/page' } });
      assert.equal(app.container.querySelector('iframe').src, 'https://example.com/page');
      await app.replace({ component: 'Iframe', props: { title: 'Host page', src: '/preview' } }, 3);
      assert.equal(app.container.querySelector('iframe').getAttribute('src'), '/preview');
    } finally {
      if (app) await app.close();
      dom.reconfigure({ url: previousUrl });
      window.jiuwenDesktop = desktop;
    }
  });
}

test('plugin text follows the interface language without remounting or resetting the switch', async () => {
  const previousLanguage = i18n.language;
  await act(async () => i18n.changeLanguage('zh'));
  const app = await setup({
    component: 'Box',
    children: [
      { component: 'Text', props: { value: { zh: '上下文上限 262.1k Token', en: 'Context limit 262.1k tokens' } } },
      {
        component: 'Button',
        props: { value: { zh: '换个风格', en: 'Shuffle' } },
        events: { 'ui.click': { id: 'shuffle', args: {} } },
      },
      {
        component: 'Switch',
        props: { defaultChecked: true, ariaLabel: { zh: '显示用量', en: 'Show usage' } },
        events: { 'ui.change': { id: 'visibility', args: {} } },
      },
      { component: 'Text', props: { value: 'Literal / 原样' } },
    ],
  });
  const control = () => app.container.querySelector('[data-testid="webui-switch"]');
  const label = () => app.container.querySelector('[data-testid="webui-button"]');
  try {
    assert.match(app.container.textContent, /上下文上限 262.1k Token/);
    assert.doesNotMatch(app.container.textContent, /Context limit|Shuffle/);
    assert.equal(label().textContent, '换个风格');
    assert.equal(control().getAttribute('aria-label'), '显示用量');
    await click(control());
    const off = control();
    const dispatchCount = app.business().length;
    await act(async () => i18n.changeLanguage('en'));
    assert.equal(control(), off);
    assert.equal(control().getAttribute('aria-checked'), 'false');
    assert.equal(control().getAttribute('aria-label'), 'Show usage');
    assert.equal(label().textContent, 'Shuffle');
    assert.match(app.container.textContent, /Context limit 262.1k tokens/);
    assert.doesNotMatch(app.container.textContent, /上下文上限|换个风格/);
    assert.match(app.container.textContent, /Literal \/ 原样/);
    assert.equal(app.business().length, dispatchCount);
    await click(label());
    assert.equal(app.business().at(-1).payload.action_id, 'shuffle');
    await act(async () => i18n.changeLanguage('zh-CN'));
    assert.equal(label().textContent, '换个风格');
    assert.equal(control().getAttribute('aria-checked'), 'false');
    await click(control());
    assert.equal(control().getAttribute('aria-checked'), 'true');
  } finally {
    await app.close();
    await act(async () => i18n.changeLanguage(previousLanguage));
  }
});

test('Avatar uses native children, styles and image priority without deriving account presentation', async () => {
  const tree = (props) => ({ component: 'Avatar', props });
  const style = { backgroundColor: '#89D7D8', color: '#FFFFFF', fontSize: 10 };
  const app = await setup(tree({ children: 'ab', alt: 'Account image', size: 20, gap: 2, style }));
  try {
    const avatar = () => app.container.querySelector('[data-testid="webui-avatar"]');
    assert.equal(avatar().textContent, 'ab', 'text is passed through without taking an initial or changing case');
    assert.equal(avatar().dataset.variant, 'text');
    assert.equal(avatar().style.backgroundColor, 'rgb(137, 215, 216)');
    assert.equal(avatar().style.color, 'rgb(255, 255, 255)');
    assert.equal(avatar().style.width, '20px');
    assert.equal(avatar().style.fontSize, '10px');
    await app.replace(tree({ children: '张三 👩‍💻', size: 20 }));
    assert.equal(avatar().textContent, '张三 👩‍💻');
    assert.equal(avatar().style.backgroundColor, '');
    assert.equal(avatar().style.color, '');
    assert.equal(avatar().style.fontSize, '18px', 'numeric size retains the native AntD default without a plugin style');
    await app.replace(tree({ src: '/logo.svg', children: 'T', alt: 'Account image', style }), 3);
    assert.equal(avatar().querySelector('img').getAttribute('src'), '/logo.svg');
    assert.equal(avatar().querySelector('img').alt, 'Account image');
    assert.equal(avatar().textContent, '', 'AntD gives an image priority over children');
    await act(async () => avatar().querySelector('img').dispatchEvent(new window.Event('error')));
    assert.equal(avatar().querySelector('img'), null);
    assert.equal(avatar().textContent, 'T', 'AntD handles image errors using the supplied children');
    await app.replace(tree({ alt: 'Description only' }), 4);
    assert.equal(avatar().textContent, '', 'alt remains an image description, not display content');
    assert.equal(avatar().getAttribute('aria-label'), null);
    await app.replace(tree({}), 5);
    assert.equal(avatar().textContent, '');
  } finally { await app.close(); }
});

test('real Button preserves Chinese labels and dispatches the declared node event', async () => {
  const app = await setup({ component: 'Button', props: { value: '确认', type: 'primary', size: 'sm' }, events });
  try {
    const button = app.container.querySelector('[data-testid="webui-button"]');
    assert.equal(button.textContent, '确认');
    assert.match(button.className, /ant-btn-primary/);
    assert.match(button.className, /ant-btn-sm/);
    assert.equal(app.business().length, 0);
    await click(button);
    assert.equal(app.business().length, 1);
    const request = app.business()[0];
    assert.equal(request.event, 'ui.click');
    assert.deepEqual(request.payload, {
      node_key: '0', revision: 1, action_id: 'confirm', args: { source: 'toolbar' }, data: {},
    });
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.equal(button.disabled, true);
    await click(button);
    assert.equal(app.business().length, 1);
    await act(async () => app.transport.setConnectionState('connected'));
    await click(button);
    assert.equal(app.business().length, 2);
  } finally { await app.close(); }
});

for (const component of ['Button', 'Text']) {
  test(`${component} uses value, preserves empty text and rejects children without removing the last valid frame`, async () => {
    const app = await setup({ component, props: { value: '有效文案' } });
    try {
      const selector = component === 'Button' ? '[data-testid="webui-button"]' : '[data-testid="webui-text"]';
      assert.equal(app.container.querySelector(selector).textContent, '有效文案');
      await app.replace({ component, props: { children: '无效文案' } }, 2);
      assert.equal(app.container.querySelector(selector).textContent, '有效文案');
      await app.replace({ component, props: { value: '' } }, 3);
      assert.equal(app.container.querySelector(selector).textContent, '');
    } finally { await app.close(); }
  });
}

test('theme initialization preserves cached instances and mounts each host once', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  transport.injectRenderFrame(frame({ component: 'Button', props: { value: 'Cached' }, events }));
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(WebUIProvider, { runtime, registry: componentRendererRegistry },
      createElement(WebUIHost, { sessionId: target.session_id, slotId: target.slot_id }))));
    assert.equal(container.querySelector('[data-testid="webui-button"]')?.textContent, 'Cached');
    assert.equal(transport.dispatches.filter((request) => request.event === 'ui.ready').length, 1);
    await click(container.querySelector('button'));
    assert.equal(transport.dispatches.filter((request) => request.event === 'ui.click').length, 1);
  } finally {
    await act(async () => root.unmount()); runtime.dispose(); container.remove();
  }
});

test('text Button preserves labels and selected variants without host artwork', async () => {
  const app = await setup({ component: 'Button', props: { value: '导出报告', type: 'text' }, events });
  try {
    const button = app.container.querySelector('[data-testid="webui-button"]');
    assert.equal(button.textContent, '导出报告');
    assert.equal(button.querySelector('svg'), null);
    assert.match(button.className, /ant-btn-text/);
    await click(button);
    assert.equal(app.business().length, 1);
    await app.replace({ component: 'Button', props: { value: '删除记录', danger: true }, events });
    assert.equal(app.container.querySelector('button').textContent, '删除记录');
    assert.match(app.container.querySelector('button').className, /ant-btn-dangerous/);
  } finally { await app.close(); }
});

test('Icon renders whitelisted artwork at theme sizes with optional labels', async () => {
  const app = await setup({ component: 'Icon', props: { name: 'search' } });
  const icon = () => app.container.querySelector('[data-testid="webui-icon"]');
  try {
    assert.equal(icon().tagName.toLowerCase(), 'svg');
    assert.equal(icon().getAttribute('data-variant'), 'search');
    assert.equal(icon().getAttribute('width'), '16');
    assert.equal(icon().getAttribute('height'), '16');
    assert.equal(icon().getAttribute('aria-hidden'), 'true');
    assert.equal(icon().getAttribute('role'), null);
    assert.match(icon().getAttribute('stroke'), /currentColor/);

    await app.replace({ component: 'Icon', props: { name: 'refresh', size: 'sm' } });
    assert.equal(icon().getAttribute('data-variant'), 'refresh');
    assert.equal(icon().getAttribute('width'), '12');

    await app.replace({ component: 'Icon', props: { name: 'camera', size: 'large' } }, 3);
    assert.equal(icon().getAttribute('data-variant'), 'camera');
    assert.equal(icon().getAttribute('width'), '20');

    await app.replace({ component: 'Icon', props: { name: 'search', 'aria-label': '  Search  ' } }, 4);
    assert.equal(icon().getAttribute('role'), 'img');
    assert.equal(icon().getAttribute('aria-label'), 'Search');
    assert.equal(icon().hasAttribute('aria-hidden'), false);

    // Invalid frames keep the last valid one; the protocol already rejects them.
    await app.replace({ component: 'Icon', props: { name: 'search', 'aria-label': '   ' } }, 5);
    assert.equal(icon().getAttribute('aria-label'), 'Search');
    await app.replace({ component: 'Icon', props: { name: 'not-an-icon' } }, 6);
    assert.equal(icon().getAttribute('data-variant'), 'search');
  } finally { await app.close(); }
});

test('every whitelisted icon renders themed artwork, including aliases', () => {
  const whitelist = JSON.parse(readFileSync(new URL('../src/features/webui/icons/whitelist.json', import.meta.url), 'utf8'));
  const artwork = name => {
    const element = buttonIcon(name);
    assert.ok(element, name);
    const svg = renderToStaticMarkup(element);
    assert.match(svg, /<svg/);
    assert.match(svg, /stroke="currentColor"/);
    assert.match(svg, /width="16"/);
    assert.match(svg, /aria-hidden="true"/);
    assert.match(svg, /<(path|circle|rect|line|polyline|polygon|ellipse)\b/);
    return svg.replace(/ data-variant="[^"]*"/, '');
  };
  for (const name of Object.keys(whitelist.icons)) artwork(name);
  for (const [name, target] of Object.entries(whitelist.aliases)) assert.equal(artwork(name), artwork(target));
  for (const name of ['not-an-icon', 'constructor', '__proto__', null, {}]) {
    assert.equal(buttonIcon(name), undefined);
  }
});

for (const icon of ['ellipsis', 'refresh', 'camera']) {
  test(`icon-only Button (${icon}) keeps its accessible label and node event without visible text`, async () => {
    const tree = { component: 'Button', props: {
      'aria-label': '本轮操作 / Round actions', icon, type: 'text',
    }, events };
    const app = await setup(tree);
    try {
      const button = app.container.querySelector('[data-testid="webui-button"]');
      assert.equal(button.textContent, '');
      assert.ok(button.querySelector('svg'));
      assert.equal(button.getAttribute('aria-label'), tree.props['aria-label']);
      assert.equal(button.title, tree.props['aria-label']);
      assert.match(button.className, /ant-btn-text/);
      assert.match(button.className, /ant-btn-icon-only/);
      await click(button);
      assert.equal(app.business().length, 1);
      assert.equal(app.business()[0].payload.action_id, 'confirm');
      await app.replace({ ...tree, props: { ...tree.props, value: tree.props['aria-label'] } });
      assert.equal(app.container.querySelector('button').textContent, tree.props['aria-label']);
      await act(async () => app.transport.setConnectionState('disconnected'));
      assert.equal(app.container.querySelector('button').disabled, true);
    } finally { await app.close(); }
  });
}

test('Button forwards antd link, target and loading props', async () => {
  const app = await setup({ component: 'Button', props: {
    value: '文档', href: 'https://example.com/docs', target: '_blank',
  }, events });
  try {
    const link = app.container.querySelector('[data-testid="webui-button"]');
    assert.equal(link.tagName, 'A');
    assert.equal(link.getAttribute('href'), 'https://example.com/docs');
    assert.equal(link.getAttribute('target'), '_blank');
    await app.replace({ component: 'Button', props: { value: '提交', loading: true }, events });
    const button = app.container.querySelector('[data-testid="webui-button"]');
    assert.equal(button.tagName, 'BUTTON');
    assert.match(button.className, /ant-btn-loading/);
  } finally { await app.close(); }
});

test('icon Dropdown opens locally, closes on selection and blocks disabled actions', async () => {
  const tree = { component: 'Dropdown', props: {
    icon: 'ellipsis', ariaLabel: '本轮操作 / Round actions', placement: 'topRight',
    items: [{ key: 'refresh', label: '刷新本轮' }, { key: 'details', label: '本轮详情', disabled: true }],
  }, events: { 'ui.select': { id: 'workbench.turn.select' } } };
  const app = await setup(tree);
  const trigger = () => app.container.querySelector('[data-testid="webui-dropdown-trigger"]');
  const item = (key) => document.querySelector(`[data-testid="webui-dropdown-item"][data-variant="${key}"]`);
  try {
    assert.equal(trigger().tagName, 'BUTTON');
    assert.equal(trigger().textContent, '');
    assert.ok(trigger().querySelector('svg'));
    assert.equal(trigger().getAttribute('aria-label'), tree.props.ariaLabel);
    await click(trigger());
    assert.equal(trigger().getAttribute('aria-expanded'), 'true');
    assert.equal(app.business().length, 0);
    await click(item('details'));
    assert.equal(app.business().length, 0);
    await click(item('refresh'));
    assert.equal(trigger().getAttribute('aria-expanded'), 'false');
    assert.equal(app.business().length, 1);
    assert.equal(app.business()[0].event, 'ui.select');
    assert.deepEqual(app.business()[0].payload.data, { key: 'refresh' });
    assert.equal(app.business()[0].payload.action_id, 'workbench.turn.select');
    await click(trigger());
    await pressKey(trigger(), 'Escape', 27);
    assert.equal(trigger().getAttribute('aria-expanded'), 'false');
    await click(trigger());
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.equal(trigger().disabled, true);
    assert.equal(trigger().getAttribute('aria-expanded'), 'false');
    await act(async () => app.transport.setConnectionState('connected'));
    assert.equal(trigger().getAttribute('aria-expanded'), 'false');
    await app.replace({ ...tree, props: { ...tree.props, disabled: true } });
    await click(trigger());
    assert.equal(trigger().getAttribute('aria-expanded'), 'false');
    assert.equal(app.business().length, 1);
  } finally { await app.close(); }
});

test('Dropdown keeps child triggers and supports keyboard activation without business events', async () => {
  const app = await setup({ component: 'Dropdown', props: {
    ariaLabel: 'User menu', items: [{ key: 'profile', label: 'Profile' }],
  }, children: [{ component: 'Text', props: { value: 'User' } }] });
  try {
    const trigger = app.container.querySelector('[data-testid="webui-dropdown-trigger"]');
    assert.equal(trigger.tagName, 'SPAN');
    assert.equal(trigger.textContent, 'User');
    await pressKey(trigger, 'Enter', 13);
    assert.equal(trigger.getAttribute('aria-expanded'), 'true');
    assert.equal(app.business().length, 0);
    await click(document.querySelector('[data-testid="webui-dropdown-item"][data-variant="profile"]'));
    assert.equal(trigger.getAttribute('aria-expanded'), 'false');
    assert.equal(app.business().length, 0);
  } finally { await app.close(); }
});

test('disabled and unbound buttons never dispatch actions', async () => {
  const app = await setup({ component: 'Button', props: { value: 'Disabled', disabled: true }, events });
  try {
    await click(app.container.querySelector('button'));
    assert.equal(app.business().length, 0);
    await app.replace({ component: 'Button', props: { value: 'Unbound', type: 'text' } });
    const button = app.container.querySelector('button');
    assert.equal(button.disabled, false);
    assert.match(button.className, /ant-btn-text/);
    await click(button);
    assert.equal(app.business().length, 0);
  } finally { await app.close(); }
});

const inputEvents = { 'ui.change': { id: 'edit', args: { field: 'city' } } };

const selectOptions = [{ value: 'a', label: 'Beijing' }, { value: 'b', label: 'Shanghai' }];
const selectEvents = {
  'ui.select': { id: 'choose', args: { field: 'city' } },
  'ui.search': { id: 'search' },
  'ui.clear': { id: 'clear' },
};
const selectTree = (props = {}, bindings = selectEvents) => ({
  component: 'Select', props: { options: selectOptions, showSearch: true, allowClear: true, ...props }, events: bindings,
});

test('Select handles search, selection and clearing through the existing event bindings', async () => {
  const app = await setup(selectTree({ defaultValue: 'a', ariaLabel: 'City' }));
  try {
    const control = app.container.querySelector('[data-testid="webui-select"]');
    assert.match(control.textContent, /Beijing/);
    assert.equal(app.business().length, 0);
    await changeInput(control.querySelector('input'), 'Shanghai');
    assert.deepEqual(app.business().find((request) => request.event === 'ui.search').payload.data,
      { searchValue: 'Shanghai' });
    await click(document.querySelector('.ant-select-item-option[title="Shanghai"]'));
    assert.match(control.textContent, /Shanghai/);
    assert.deepEqual(app.business().find((request) => request.event === 'ui.select').payload, {
      node_key: '0', revision: 1, action_id: 'choose', args: { field: 'city' }, data: { value: 'b' },
    });
    const clear = control.querySelector('.ant-select-clear');
    assert.ok(clear);
    await click(clear);
    assert.deepEqual(app.business().find((request) => request.event === 'ui.clear').payload.data, {});
    assert.ok(!control.textContent.includes('Shanghai'));
  } finally { await app.close(); }
});

test('Select preserves local edits without bindings, resets on replace and blocks disabled or disconnected searches', async () => {
  const app = await setup({ component: 'Select', props: { options: selectOptions, showSearch: true } });
  try {
    await changeInput(app.container.querySelector('input'), 'Shanghai');
    await click(document.querySelector('.ant-select-item-option[title="Shanghai"]'));
    assert.match(app.container.textContent, /Shanghai/);
    assert.equal(app.business().length, 0);
    await app.replace(selectTree({ value: 'forged' }));
    assert.match(app.container.textContent, /Shanghai/);
    await app.replace(selectTree({ value: 'a', disabled: true }), 3);
    assert.match(app.container.textContent, /Beijing/);
    assert.equal(app.container.querySelector('input').disabled, true);
    await changeInput(app.container.querySelector('input'), 'Blocked');
    await app.replace(selectTree({ defaultValue: 'b' }), 4);
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.equal(app.container.querySelector('input').disabled, true);
    await changeInput(app.container.querySelector('input'), 'Offline');
    assert.equal(app.business().length, 0);
    await act(async () => app.transport.setConnectionState('connected'));
    await changeInput(app.container.querySelector('input'), 'Beijing');
    assert.equal(app.business().at(-1).payload.revision, 4);
    assert.deepEqual(app.business().at(-1).payload.data, { searchValue: 'Beijing' });
  } finally { await app.close(); }
});

test('Select change sends the whole selection on choose, deselect and clear without extra bindings', async () => {
  const bindings = { 'ui.change': { id: 'update-selection', args: { field: 'cities' } } };
  const app = await setup(selectTree({ mode: 'multiple', defaultValue: ['a'] }, bindings));
  const changes = () => app.business().map((request) => request.payload.data.value);
  try {
    assert.equal(app.business().length, 0);
    await changeInput(app.container.querySelector('input'), 'Shanghai');
    await click(document.querySelector('.ant-select-item-option[title="Shanghai"]'));
    assert.deepEqual(changes(), [['a', 'b']]);
    await click(app.container.querySelector('.ant-select-selection-item[title="Beijing"] .ant-select-selection-item-remove'));
    assert.deepEqual(changes(), [['a', 'b'], ['b']]);
    await click(app.container.querySelector('.ant-select-clear'));
    assert.deepEqual(changes(), [['a', 'b'], ['b'], []]);
    assert.ok(app.business().every((request) => request.event === 'ui.change'));
    await app.replace(selectTree({ defaultValue: 'a' }, bindings));
    assert.equal(app.business().length, 3);
    await click(app.container.querySelector('.ant-select-clear'));
    assert.deepEqual(changes().at(-1), null);
    assert.equal(app.business().at(-1).payload.action_id, 'update-selection');
    await app.replace(selectTree({ disabled: true }, bindings), 3);
    assert.equal(app.container.querySelector('input').disabled, true);
    await changeInput(app.container.querySelector('input'), 'Shanghai');
    assert.equal(app.business().length, 4);
    await app.replace(selectTree({}, bindings), 4);
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.equal(app.container.querySelector('input').disabled, true);
    await changeInput(app.container.querySelector('input'), 'Shanghai');
    assert.equal(app.business().length, 4);
  } finally { await app.close(); }
});

test('Select tags change reports custom tags and respects maxCount', async () => {
  const app = await setup(selectTree({ options: [], mode: 'tags', maxCount: 1 }, { 'ui.change': { id: 'tags' } }));
  try {
    const input = () => app.container.querySelector('input');
    await changeInput(input(), 'custom');
    await pressEnter(input());
    assert.deepEqual(app.business().map((request) => request.payload.data), [{ value: ['custom'] }]);
    await changeInput(input(), 'second');
    await pressEnter(input());
    assert.ok(app.business().every((request) => request.payload.data.value.length <= 1));
    assert.ok(!app.container.textContent.includes('second'));
  } finally { await app.close(); }
});

const treeSelectOptions = [
  { value: 'alpha', title: 'Alpha', children: [{ value: 'alpha-one', title: 'Alpha One' }] },
  { value: 'beta', title: 'Beta' },
];
const treeSelectEvents = {
  'ui.select': { id: 'pick', args: { field: 'node' } },
  'ui.change': { id: 'edit', args: { field: 'node' } },
  'ui.clear': { id: 'clear' },
};
const treeSelectTree = (props = {}, bindings = treeSelectEvents) => ({
  component: 'TreeSelect', props: { treeData: treeSelectOptions, showSearch: true, allowClear: true, ...props },
  events: bindings,
});

test('TreeSelect sends the picked node, the whole selection and clearing through separate bindings', async () => {
  const app = await setup(treeSelectTree({ ariaLabel: 'Node' }));
  try {
    const control = app.container.querySelector('[data-testid="webui-tree-select"]');
    assert.equal(app.business().length, 0);
    await changeInput(control.querySelector('input'), 'Beta');
    await click(document.querySelector('.ant-select-tree-node-content-wrapper'));
    assert.deepEqual(app.business().map((request) => request.event).sort(), ['ui.change', 'ui.select']);
    assert.deepEqual(app.business().find((request) => request.event === 'ui.select').payload, {
      node_key: '0', revision: 1, action_id: 'pick', args: { field: 'node' }, data: { value: 'beta' },
    });
    assert.deepEqual(app.business().find((request) => request.event === 'ui.change').payload.data, { value: 'beta' });
    assert.match(control.textContent, /Beta/);
    const clear = control.querySelector('.ant-select-clear');
    assert.ok(clear);
    await click(clear);
    assert.deepEqual(app.business().find((request) => request.event === 'ui.clear').payload.data, {});
    assert.deepEqual(app.business().filter((request) => request.event === 'ui.change').at(-1).payload.data,
      { value: null });
  } finally { await app.close(); }
});

test('TreeSelect keeps local selection without bindings, resets on valid replace and disables on disconnect', async () => {
  const app = await setup({ component: 'TreeSelect', props: { treeData: treeSelectOptions, showSearch: true } });
  try {
    await changeInput(app.container.querySelector('input'), 'Alpha');
    await click(document.querySelector('.ant-select-tree-node-content-wrapper'));
    assert.match(app.container.textContent, /Alpha/);
    assert.equal(app.business().length, 0);
    await app.replace(treeSelectTree({ value: 'beta' }));
    assert.match(app.container.textContent, /Beta/);
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.equal(app.container.querySelector('input').disabled, true);
  } finally { await app.close(); }
});

test('TreeSelect sends the whole checked selection for change in treeCheckable mode', async () => {
  const flatTree = [{ value: 'alpha', title: 'Alpha' }, { value: 'beta', title: 'Beta' }];
  const app = await setup(treeSelectTree({ treeData: flatTree, value: ['alpha'], treeCheckable: true },
    { 'ui.change': { id: 'edit', args: { field: 'node' } } }));
  try {
    await changeInput(app.container.querySelector('input'), 'Beta');
    await click(document.querySelector('.ant-select-tree-node-content-wrapper'));
    const change = app.business().find((request) => request.event === 'ui.change');
    assert.deepEqual([...change.payload.data.value].sort(), ['alpha', 'beta']);
    assert.equal(app.business().filter((request) => request.event === 'ui.select').length, 0);
  } finally { await app.close(); }
});

const sliderTree = (props = {}, bindings = { 'ui.change': { id: 'slide', args: { field: 'level' } } }) => ({
  component: 'Slider', props, events: bindings,
});

test('Slider sends every keyboard change as a number and stops when disconnected', async () => {
  const app = await setup(sliderTree({ value: 10, ariaLabel: 'Level' }));
  try {
    const handle = () => app.container.querySelector('[role="slider"]');
    assert.equal(handle().getAttribute('aria-valuenow'), '10');
    await pressKey(handle(), 'ArrowRight', 39);
    await pressKey(handle(), 'ArrowRight', 39);
    assert.deepEqual(app.business().map((request) => request.payload.data.value), [11, 12]);
    assert.deepEqual(app.business()[0].payload, {
      node_key: '0', revision: 1, action_id: 'slide', args: { field: 'level' }, data: { value: 11 },
    });
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.ok(app.container.querySelector('.ant-slider-disabled'));
    await pressKey(handle(), 'ArrowRight', 39);
    assert.equal(app.business().length, 2);
  } finally { await app.close(); }
});

test('Slider keeps its local value without bindings and resets on valid replace', async () => {
  const app = await setup({ component: 'Slider', props: { value: 20 } });
  try {
    const handle = () => app.container.querySelector('[role="slider"]');
    await pressKey(handle(), 'ArrowRight', 39);
    assert.equal(handle().getAttribute('aria-valuenow'), '21');
    assert.equal(app.business().length, 0);
    await app.replace({ component: 'Slider', props: { value: 60 } });
    assert.equal(handle().getAttribute('aria-valuenow'), '60');
    await pressKey(handle(), 'ArrowLeft', 37);
    assert.equal(handle().getAttribute('aria-valuenow'), '59');
    assert.equal(app.business().length, 0);
  } finally { await app.close(); }
});

const tabsTree = (props = {}) => ({
  component: 'Tabs',
  events: { 'ui.change': { id: 'switch', args: { field: 'tab' } } },
  props: {
    items: [
      { key: 'one', label: 'One', children: [
        { component: 'Input', props: { defaultValue: 'draft' },
          events: { 'ui.change': { id: 'edit', args: { field: 'text' } } } },
      ] },
      { key: 'two', label: 'Two', children: [{ component: 'Text', props: { value: 'Second' } }] },
    ],
    ...props,
  },
});
const tabLabel = (app, key) => app.container.querySelector(`[data-testid="webui-tab-label"][data-variant="${key}"]`);

test('Tabs sends the active key through the existing change binding', async () => {
  const app = await setup(tabsTree());
  try {
    assert.deepEqual([...app.container.querySelectorAll('[data-testid="webui-tab-label"]')]
      .map((label) => label.getAttribute('data-variant')), ['one', 'two']);
    assert.equal(app.business().length, 0);
    await click(tabLabel(app, 'two'));
    assert.deepEqual(app.business().at(-1).payload, {
      node_key: '0', revision: 1, action_id: 'switch', args: { field: 'tab' }, data: { activeKey: 'two' },
    });
  } finally { await app.close(); }
});

test('Tabs keeps mounted content state across switching and only sends its own change', async () => {
  const app = await setup(tabsTree());
  try {
    const input = () => app.container.querySelector('input');
    await changeInput(input(), 'kept');
    const dispatched = app.business().length;
    await click(tabLabel(app, 'two'));
    assert.equal(app.business().length, dispatched + 1);
    assert.deepEqual(app.business().at(-1).payload.data, { activeKey: 'two' });
    await click(tabLabel(app, 'one'));
    assert.equal(input().value, 'kept');
    assert.deepEqual(app.business().at(-1).payload.data, { activeKey: 'one' });
  } finally { await app.close(); }
});

test('Tabs reinitializes hidden content when destroyOnHidden is set', async () => {
  const app = await setup(tabsTree({ destroyOnHidden: true }));
  try {
    const input = () => app.container.querySelector('input');
    await changeInput(input(), 'discard');
    await click(tabLabel(app, 'two'));
    await click(tabLabel(app, 'one'));
    assert.equal(input().value, 'draft');
  } finally { await app.close(); }
});

test('WebUIHost inherits the slot session and applies the requested layout', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(WebUIProvider, { runtime, registry: componentRendererRegistry },
      createElement(WebUISlotProvider, { sessionId: 'sess-1' },
        createElement(WebUIHost, { slotId: 'conversation.input.above', layout: 'row' })))));
    await act(async () => transport.injectRenderFrame(frame(
      { component: 'Text', props: { value: 'Context' } }, 1, { ...target },
    )));
    const host = container.querySelector('[data-testid="webui-host"]');
    assert.ok(host);
    assert.match(host.className, /flex-row/);
    assert.equal(host.getAttribute('data-variant'), 'conversation.input.above');
    assert.equal(host.getAttribute('data-connected'), 'true');
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    container.remove();
  }
});

const switchTree = (props = {}, bindings = { 'ui.change': { id: 'toggle', args: { field: 'enabled' } } }) => ({
  component: 'Switch', props, events: bindings,
});

test('Switch click sends the new checked state independently of change and respects disabled states', async () => {
  const bindings = { 'ui.click': { id: 'inspect', args: { field: 'enabled' } } };
  const app = await setup(switchTree({ defaultChecked: true }, bindings));
  try {
    assert.equal(app.business().length, 0);
    await click(app.container.querySelector('[role="switch"]'));
    assert.equal(app.container.querySelector('[role="switch"]').getAttribute('aria-checked'), 'false');
    assert.equal(app.business()[0].event, 'ui.click');
    assert.deepEqual(app.business()[0].payload, {
      node_key: '0', revision: 1, action_id: 'inspect', args: { field: 'enabled' }, data: { checked: false },
    });
    await app.replace(switchTree({ checked: false }, { ...bindings, 'ui.change': { id: 'toggle' } }));
    await click(app.container.querySelector('[role="switch"]'));
    assert.deepEqual(app.business().slice(1).map((request) => [request.event, request.payload.action_id, request.payload.data]), [
      ['ui.change', 'toggle', { checked: true }], ['ui.click', 'inspect', { checked: true }],
    ]);
    await app.replace(switchTree({ disabled: true }, bindings), 3);
    await click(app.container.querySelector('[role="switch"]'));
    await app.replace(switchTree({ loading: true }, bindings), 4);
    await click(app.container.querySelector('[role="switch"]'));
    await app.replace(switchTree({ checked: false }, bindings), 5);
    await act(async () => app.transport.setConnectionState('disconnected'));
    await click(app.container.querySelector('[role="switch"]'));
    assert.equal(app.business().length, 3);
    await act(async () => app.transport.setConnectionState('connected'));
    await click(app.container.querySelector('[role="switch"]'));
    assert.equal(app.business().at(-1).payload.revision, 5);
    assert.deepEqual(app.business().at(-1).payload.data, { checked: true });
  } finally { await app.close(); }
});

test('Switch change initializes without dispatch and sends boolean states with the declared action', async () => {
  const app = await setup(switchTree({ defaultChecked: true, ariaLabel: 'Enabled',
    checkedChildren: 'On', unCheckedChildren: 'Off', size: 'small' }));
  try {
    const control = app.container.querySelector('[data-testid="webui-switch"]');
    assert.equal(control.getAttribute('aria-label'), 'Enabled');
    assert.equal(control.getAttribute('aria-checked'), 'true');
    assert.match(control.className, /ant-switch-small/);
    assert.equal(app.business().length, 0);
    await click(control);
    assert.equal(control.getAttribute('aria-checked'), 'false');
    assert.equal(app.business()[0].event, 'ui.change');
    assert.deepEqual(app.business()[0].payload, {
      node_key: '0', revision: 1, action_id: 'toggle', args: { field: 'enabled' }, data: { checked: false },
    });
    await click(control);
    assert.deepEqual(app.business().map((request) => request.payload.data), [{ checked: false }, { checked: true }]);
  } finally { await app.close(); }
});

test('Switch change respects replacement, unbound edits, disabled, loading and disconnection', async () => {
  const app = await setup(switchTree({ checked: true, defaultChecked: false }));
  try {
    assert.equal(app.container.querySelector('[role="switch"]').getAttribute('aria-checked'), 'true');
    await app.replace({ component: 'Switch', props: { checked: false } });
    await click(app.container.querySelector('[role="switch"]'));
    assert.equal(app.container.querySelector('[role="switch"]').getAttribute('aria-checked'), 'true');
    assert.equal(app.business().length, 0);
    await app.replace(switchTree({ checked: false, disabled: true }), 3);
    assert.equal(app.container.querySelector('[role="switch"]').disabled, true);
    await click(app.container.querySelector('[role="switch"]'));
    await app.replace(switchTree({ loading: true }), 4);
    assert.equal(app.container.querySelector('[role="switch"]').disabled, true);
    await click(app.container.querySelector('[role="switch"]'));
    await app.replace(switchTree({ checked: false }), 5);
    assert.equal(app.container.querySelector('[role="switch"]').getAttribute('aria-checked'), 'false');
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.equal(app.container.querySelector('[role="switch"]').disabled, true);
    await click(app.container.querySelector('[role="switch"]'));
    assert.equal(app.business().length, 0);
    await act(async () => app.transport.setConnectionState('connected'));
    await click(app.container.querySelector('[role="switch"]'));
    assert.equal(app.business().at(-1).payload.revision, 5);
    assert.deepEqual(app.business().at(-1).payload.data, { checked: true });
  } finally { await app.close(); }
});

const inputTree = (props = {}, bindings = inputEvents) => ({ component: 'Input', props, events: bindings });
const inputClickEvents = { 'ui.click': { id: 'inspect', args: { field: 'city' } } };

test('Input click sends the current draft and allows read-only clicks while blocking disabled and disconnected inputs', async () => {
  const bindings = inputClickEvents;
  const app = await setup(inputTree({ value: 'Initial' }, bindings));
  try {
    const input = app.container.querySelector('input');
    await changeInput(input, 'Edited');
    assert.equal(app.business().length, 0);
    await click(input);
    assert.deepEqual(app.business()[0].payload, {
      node_key: '0', revision: 1, action_id: 'inspect', args: { field: 'city' }, data: { value: 'Edited' },
    });
    await app.replace(inputTree({ value: 'Read only', readOnly: true }, bindings));
    await click(app.container.querySelector('input'));
    assert.deepEqual(app.business().at(-1).payload.data, { value: 'Read only' });
    await app.replace(inputTree({ value: 'Disabled', disabled: true }, bindings), 3);
    await click(app.container.querySelector('input'));
    await app.replace(inputTree({ value: 'Live' }, bindings), 4);
    await act(async () => app.transport.setConnectionState('disconnected'));
    await click(app.container.querySelector('input'));
    assert.equal(app.business().length, 2);
    await act(async () => app.transport.setConnectionState('connected'));
    await click(app.container.querySelector('input'));
    assert.equal(app.business().at(-1).event, 'ui.click');
    assert.equal(app.business().at(-1).payload.revision, 4);
    assert.deepEqual(app.business().at(-1).payload.data, { value: 'Live' });
  } finally { await app.close(); }
});

test('Input sends exact change data and clear values without dispatching on mount, click, Enter or blur', async () => {
  const app = await setup(inputTree({ value: '北京', ariaLabel: 'City', allowClear: true, showCount: true, maxLength: 20, autoComplete: 'off' }));
  try {
    const input = app.container.querySelector('[data-testid="webui-input"]');
    assert.equal(input.value, '北京');
    assert.equal(input.getAttribute('aria-label'), 'City');
    assert.equal(input.getAttribute('autocomplete'), 'off');
    assert.equal(input.maxLength, 20);
    assert.equal(app.business().length, 0);
    await click(input);
    await act(async () => {
      input.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Enter', keyCode: 13, bubbles: true }));
      input.dispatchEvent(new window.FocusEvent('focusout', { bubbles: true }));
    });
    assert.equal(app.business().length, 0);
    await changeInput(input, '上海');
    assert.equal(input.value, '上海');
    assert.deepEqual(app.business()[0].payload, {
      node_key: '0', revision: 1, action_id: 'edit', args: { field: 'city' },
      data: { value: '上海', isComposing: false },
    });
    await click(app.container.querySelector('.ant-input-clear-icon'));
    assert.equal(input.value, '');
    assert.deepEqual(app.business().map((request) => [request.event, request.payload.data]), [
      ['ui.change', { value: '上海', isComposing: false }],
      ['ui.change', { value: '', isComposing: false }],
    ]);
  } finally { await app.close(); }
});

test('Input sends composition drafts and one committed value, then accepts later edits', async () => {
  const app = await setup(inputTree());
  try {
    const input = app.container.querySelector('input');
    await act(async () => input.dispatchEvent(new window.CompositionEvent('compositionstart', { bubbles: true })));
    await changeInput(input, '北', true);
    await changeInput(input, '北京', true);
    await act(async () => input.dispatchEvent(new window.CompositionEvent('compositionend', { bubbles: true })));
    await changeInput(input, '北京');
    assert.deepEqual(app.business().map((request) => request.payload.data), [
      { value: '北', isComposing: true }, { value: '北京', isComposing: true },
      { value: '北京', isComposing: false },
    ]);
    await changeInput(input, '北京市');
    await changeInput(input, '北京');
    assert.equal(app.business().length, 5);
    assert.deepEqual(app.business().at(-1).payload.data, { value: '北京', isComposing: false });
  } finally { await app.close(); }
});

test('Input remains editable without a change binding and resets only for valid replacements', async () => {
  const app = await setup({ component: 'Input', props: { defaultValue: 'Initial' } });
  try {
    const input = app.container.querySelector('input');
    await changeInput(input, 'Draft');
    assert.equal(input.value, 'Draft');
    assert.equal(app.business().length, 0);
    await app.replace(inputTree({ value: 123 }));
    assert.ok(app.container.querySelector('input') === input);
    assert.equal(input.value, 'Draft');
    await app.replace(inputTree({ value: 'Server' }), 3);
    assert.ok(app.container.querySelector('input') !== input);
    assert.equal(app.container.querySelector('input').value, 'Server');
    assert.equal(app.business().length, 0);
  } finally { await app.close(); }
});

test('Input blocks disabled, read-only and disconnected edits, then restores live changes on reconnect', async () => {
  const app = await setup(inputTree({ value: 'Initial', disabled: true }));
  try {
    await changeInput(app.container.querySelector('input'), 'Disabled');
    assert.equal(app.business().length, 0);
    await app.replace(inputTree({ value: 'Read only', readOnly: true }));
    const input = app.container.querySelector('input');
    assert.equal(input.readOnly, true);
    await changeInput(input, 'Blocked');
    assert.equal(input.value, 'Read only');
    assert.equal(app.business().length, 0);
    await app.replace(inputTree({ value: 'Live' }), 3);
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.equal(app.container.querySelector('input').disabled, true);
    await changeInput(app.container.querySelector('input'), 'Offline');
    assert.equal(app.business().length, 0);
    await act(async () => app.transport.setConnectionState('connected'));
    await changeInput(app.container.querySelector('input'), 'Online');
    assert.deepEqual(app.business().at(-1).payload.data, { value: 'Online', isComposing: false });
    assert.equal(app.business().at(-1).payload.revision, 3);
  } finally { await app.close(); }
});

test('Input sends continuous edits while earlier change receipts are pending', async () => {
  const app = await setup(inputTree(), target, { autoAccept: false });
  try {
    const input = app.container.querySelector('input');
    for (const value of ['a', 'ab', 'a', 'ab']) await changeInput(input, value);
    assert.deepEqual(app.business().map((request) => request.payload.data.value), ['a', 'ab', 'a', 'ab']);
    assert.equal(input.value, 'ab');
    await act(async () => {
      while (app.transport.pendingDispatchCount) {
        app.transport.resolveNextDispatch({ accepted: true, status: 'handled' });
      }
    });
    assert.equal(app.business().length, 4);
  } finally { await app.close(); }
});

const inputSubmitEvents = { 'ui.press_enter': { id: 'submit', args: { field: 'city' } } };

test('Input submits the current draft, including empty and repeated values, while receipts are pending', async () => {
  const app = await setup(inputTree({ value: 'Initial' }, inputSubmitEvents), target, { autoAccept: false });
  try {
    const input = app.container.querySelector('input');
    await changeInput(input, 'Edited');
    await click(input);
    assert.equal(app.business().length, 0);
    await pressEnter(input);
    await pressEnter(input);
    await changeInput(input, '');
    await pressEnter(input);
    assert.deepEqual(app.business().map((request) => [request.event, request.payload]), [
      ['ui.press_enter', { node_key: '0', revision: 1, action_id: 'submit', args: { field: 'city' },
        data: { value: 'Edited' } }],
      ['ui.press_enter', { node_key: '0', revision: 1, action_id: 'submit', args: { field: 'city' },
        data: { value: 'Edited' } }],
      ['ui.press_enter', { node_key: '0', revision: 1, action_id: 'submit', args: { field: 'city' },
        data: { value: '' } }],
    ]);
  } finally {
    await act(async () => {
      while (app.transport.pendingDispatchCount) {
        app.transport.resolveNextDispatch({ accepted: true, status: 'handled' });
      }
    });
    await app.close();
  }
});

test('Input ignores Enter during composition and keyCode 229, then submits after composition ends', async () => {
  const app = await setup(inputTree({}, { ...inputEvents, ...inputSubmitEvents }));
  try {
    const input = app.container.querySelector('input');
    await act(async () => input.dispatchEvent(new window.CompositionEvent('compositionstart', { bubbles: true })));
    await changeInput(input, '北京', true);
    await pressEnter(input);
    await act(async () => input.dispatchEvent(new window.CompositionEvent('compositionend', { bubbles: true })));
    await pressEnter(input, { isComposing: true });
    await pressEnter(input, { keyCode: 229 });
    assert.deepEqual(app.business().map((request) => request.event), ['ui.change', 'ui.change']);
    await pressEnter(input);
    assert.deepEqual(app.business().map((request) => [request.event, request.payload.data]), [
      ['ui.change', { value: '北京', isComposing: true }],
      ['ui.change', { value: '北京', isComposing: false }],
      ['ui.press_enter', { value: '北京' }],
    ]);
  } finally { await app.close(); }
});

test('Input blocks Enter when disabled, read-only or disconnected and uses the replacement value after reconnect', async () => {
  const app = await setup(inputTree({ value: 'Disabled', disabled: true }, inputSubmitEvents));
  try {
    await pressEnter(app.container.querySelector('input'));
    await app.replace(inputTree({ value: 'Read only', readOnly: true }, inputSubmitEvents));
    await pressEnter(app.container.querySelector('input'));
    await app.replace(inputTree({ value: 'Server' }, inputSubmitEvents), 3);
    await act(async () => app.transport.setConnectionState('disconnected'));
    await pressEnter(app.container.querySelector('input'));
    assert.equal(app.business().length, 0);
    await act(async () => app.transport.setConnectionState('connected'));
    await pressEnter(app.container.querySelector('input'));
    assert.equal(app.business().at(-1).payload.revision, 3);
    assert.deepEqual(app.business().at(-1).payload.data, { value: 'Server' });
  } finally { await app.close(); }
});

async function verifyInputTransport(eventName) {
  const bindings = { 'ui.change': inputEvents, 'ui.press_enter': inputSubmitEvents, 'ui.click': inputClickEvents }[eventName];
  const subscriptions = new Map();
  const requests = [];
  const client = {
    on(name, handler) { subscriptions.set(name, handler); return () => subscriptions.delete(name); },
    onStateChange() { return () => {}; },
    getState: () => 'ready',
    async request(method, params) {
      requests.push([method, params]);
      if (params.event === eventName) {
        subscriptions.get('webui.render')({ payload: frame(inputTree({ value: 'Confirmed' }, bindings), 2) });
      }
      return { accepted: true, status: 'handled' };
    },
  };
  const app = createWebUIAppRuntime({ client, catalog: componentCatalog, registry: componentRendererRegistry });
  app.start();
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(WebUIProvider, { runtime: app.runtime, registry: app.registry },
      createElement(WebUIHost, { sessionId: target.session_id, slotId: target.slot_id }))));
    await act(async () => subscriptions.get('webui.render')({ payload: frame(inputTree({ value: 'Initial' }, bindings)) }));
    await changeInput(container.querySelector('input'), 'Edited');
    if (eventName === 'ui.press_enter') await pressEnter(container.querySelector('input'));
    if (eventName === 'ui.click') await click(container.querySelector('input'));
    const [method, params] = requests.find(([, request]) => request.event === eventName);
    assert.equal(method, 'webui.dispatch');
    assert.deepEqual(params, {
      event: eventName, session_id: 'sess-1',
      target: { slot_id: target.slot_id, package_id: target.package_id, instance_key: target.instance_key },
      payload: { node_key: '0', revision: 1, action_id: bindings[eventName].id, args: { field: 'city' },
        data: eventName === 'ui.change' ? { value: 'Edited', isComposing: false } : { value: 'Edited' } },
    });
    assert.equal(container.querySelector('input').value, 'Confirmed');
  } finally {
    await act(async () => root.unmount()); app.dispose(); container.remove();
    assert.equal(subscriptions.size, 0);
  }
}
for (const eventName of ['ui.change', 'ui.press_enter', 'ui.click']) {
  test(`Input ${eventName} uses the WebClient transport envelope and applies returned render frames`,
    () => verifyInputTransport(eventName));
}

test('host appearance supplies native layout, selection, icons and accessible icon-only buttons', async () => {
  const result = createProtocolPipeline(componentCatalog).processRenderFrame(frame({
    component: 'Box', children: [{ component: 'Button', props: { value: 'Open' }, events }],
  }));
  assert.equal(result.ok, true);
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  const sent = [];
  const render = (connected) => root.render(createElement(WebUISchemaRenderer, {
    tree: result.value.tree, registry: componentRendererRegistry, renderGeneration: 1,
    context: {
      instanceKey: 'host', connected, selected: true,
      styles: {
        Box: { unstyled: true, className: 'host-layout', style: { display: 'grid' } },
        Button: { unstyled: true, className: 'host-action', activeClassName: 'selected', hideLabel: true,
          icon: createElement('span', { 'data-testid': 'host-icon' }, '+') },
      },
    },
    bindNode: () => ({ release() {}, async emit(event, data) { sent.push([event, data]); return { status: 'handled' }; } }),
  }));
  try {
    await act(async () => render(true));
    const box = container.querySelector('[data-testid="webui-box"]');
    const button = container.querySelector('button');
    assert.equal(box.tagName, 'DIV'); assert.equal(box.style.display, 'grid');
    assert.equal(button.className, 'host-action selected');
    assert.equal(button.getAttribute('aria-label'), 'Open');
    assert.equal(button.getAttribute('aria-pressed'), 'true');
    assert.equal(container.querySelector('[data-testid="webui-button-label"]'), null);
    assert.equal(container.querySelector('[data-testid="host-icon"]').textContent, '+');
    await click(button); assert.deepEqual(sent, [['ui.click', {}]]);
    await act(async () => render(false));
    assert.equal(button.disabled, true);
    await click(button); assert.equal(sent.length, 1);
  } finally { await act(async () => root.unmount()); container.remove(); }
});

test('host theme maps semantic values, updates on root changes and localizes library labels', async () => {
  const element = document.documentElement;
  element.style.setProperty('--color-action-primary', '#345678');
  element.style.setProperty('--color-text-primary', '#234567');
  element.style.setProperty('--color-action-secondary', '#f4f4f5');
  element.style.setProperty('--font-size-base', '15px');
  element.style.setProperty('--radius-md', '9px');
  const resolved = resolveWebUITheme(getComputedStyle(element), false);
  assert.equal(resolved.token.colorPrimary, '#345678');
  assert.equal(resolved.token.fontSize, 15);
  assert.equal(resolved.token.borderRadius, 9);
  assert.equal(resolved.token.colorError, undefined);
  assert.equal(resolved.components.Button.textHoverBg, '#f4f4f5');
  assert.equal(resolveWebUITheme({ getPropertyValue: () => '' }, false).components.Button.textHoverBg, undefined);
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  let token;
  function Probe() { token = theme.useToken().token; return null; }
  try {
    await act(async () => i18n.changeLanguage('en'));
    await act(async () => root.render(createElement(WebUIThemeProvider, null,
      createElement(Probe), createElement(componentRendererRegistry.get('Empty'), { node: { props: {} } }))));
    assert.equal(token.colorPrimary, '#345678');
    assert.equal(token.colorText, '#234567');
    assert.equal(token.fontSize, 15);
    assert.match(container.textContent, /No data/);
    await act(async () => {
      element.style.setProperty('--color-action-primary', '#765432');
      element.style.setProperty('--color-action-secondary', '#303030');
    });
    assert.equal(token.colorPrimary, '#765432');
    assert.equal(resolveWebUITheme(getComputedStyle(element), true).components.Button.textHoverBg, '#303030');
    await act(async () => { element.dataset.colorMode = 'dark'; });
    assert.notEqual(token.colorPrimary, '#765432');
    assert.equal(resolveWebUITheme(getComputedStyle(element), true).algorithm, theme.darkAlgorithm);
    await act(async () => i18n.changeLanguage('zh'));
    assert.match(container.textContent, /暂无数据/);
  } finally {
    await act(async () => root.unmount()); container.remove();
    element.removeAttribute('style'); delete element.dataset.colorMode;
    await i18n.changeLanguage('zh');
  }
});

test('Text respects theme sizes, semantic types and host style precedence', async () => {
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(componentRendererRegistry.get('Text'), {
      node: { props: { value: 'Status', type: 'danger', size: 'lg', strong: true, code: true, ellipsis: true } },
      appearance: { className: 'host-text', style: { fontSize: 21, fontWeight: 500, fontFamily: 'serif', overflow: 'visible' } },
    })));
    const text = container.querySelector('[data-testid="webui-text"]');
    assert.equal(text.textContent, 'Status');
    assert.match(text.className, /ant-typography-danger/);
    assert.match(text.className, /host-text/);
    assert.equal(text.style.fontSize, '21px');
    assert.equal(text.style.fontWeight, '500');
    assert.equal(text.style.fontFamily, 'serif');
    assert.equal(getComputedStyle(text).overflow, 'visible');
  } finally { await act(async () => root.unmount()); container.remove(); }
});

test('Text semantic types and Button variant/type props map to antd classes', async () => {
  const app = await setup({ component: 'Text', props: { value: 'Status', type: 'secondary' } });
  const text = () => app.container.querySelector('[data-testid="webui-text"]');
  try {
    assert.match(text().className, /ant-typography-secondary/);
    await app.replace({ component: 'Text', props: { value: 'Status', type: 'warning' } }, 2);
    assert.match(text().className, /ant-typography-warning/);
    await app.replace({ component: 'Text', props: { value: 'Status', type: 'danger' } }, 3);
    assert.match(text().className, /ant-typography-danger/);
    await app.replace({ component: 'Text', props: { value: 'Status' } }, 4);
    assert.doesNotMatch(text().className, /ant-typography-danger/);
    await app.replace({ component: 'Text', props: { value: 'Status', type: 'success' } }, 5);
    assert.match(text().className, /ant-typography-success/);
    await app.replace({ component: 'Button', props: { value: 'Secondary', variant: 'filled', size: 'large' } }, 6);
    const button = app.container.querySelector('[data-testid="webui-button"]');
    assert.match(button.className, /ant-btn-variant-filled/);
    assert.match(button.className, /ant-btn-lg/);
    await app.replace({ component: 'Button', props: { value: 'Default', size: 'lg' } }, 7);
    assert.doesNotMatch(app.container.querySelector('button').className, /ant-btn-variant-filled/);
    assert.match(app.container.querySelector('button').className, /ant-btn-lg/);
  } finally { await app.close(); }
});

test('Text renders antd typography modifiers and ellipsis from normalized props', async () => {
  const app = await setup({
    component: 'Text',
    props: {
      value: 'First\nSecond\nThird',
      code: true,
      strong: true,
      italic: true,
      underline: true,
      delete: true,
      mark: true,
      keyboard: true,
      ellipsis: true,
    },
  });
  try {
    const text = app.container.querySelector('[data-testid="webui-text"]');
    assert.ok(text.querySelector('code'));
    assert.ok(text.querySelector('strong'));
    assert.ok(text.querySelector('i'));
    assert.ok(text.querySelector('u'));
    assert.ok(text.querySelector('del'));
    assert.ok(text.querySelector('mark'));
    assert.ok(text.querySelector('kbd'));
    assert.match(text.className, /ant-typography-ellipsis/);
    await app.replace({ component: 'Text', props: { value: 'Plain', code: false, ellipsis: false } });
    const plain = app.container.querySelector('[data-testid="webui-text"]');
    assert.equal(plain.querySelector('code'), null);
    assert.doesNotMatch(plain.className, /ant-typography-ellipsis/);
  } finally { await app.close(); }
});

test('Iframe releases inactive documents and resets loading on source changes and reconnection', async () => {
  const tree = (src) => ({ component: 'Iframe', props: { src, title: 'Preview' } });
  const app = await setup(tree('https://example.com/first'), { ...target, slot_id: 'conversation.panel.right' });
  const state = () => app.container.querySelector('[data-testid="webui-iframe-state"]');
  const iframe = () => app.container.querySelector('iframe');
  try {
    const first = iframe();
    assert.equal(first.getAttribute('sandbox'), 'allow-scripts allow-forms allow-popups allow-modals allow-downloads');
    assert.equal(first.getAttribute('referrerpolicy'), 'no-referrer');
    assert.equal(first.title, 'Preview');
    assert.equal(state().dataset.variant, 'loading');
    assert.equal(getComputedStyle(state()).pointerEvents, 'none', 'loading notice must not intercept iframe clicks');
    assert.equal(getComputedStyle(state()).inset, '12px 12px auto auto', 'loading notice must not cover the document');
    await act(async () => first.dispatchEvent(new window.Event('load')));
    assert.ok(!state(), 'loaded frame hides its notice');
    await app.activity(false);
    assert.ok(!iframe(), 'inactive content releases its iframe'); assert.equal(state().dataset.variant, 'inactive');
    assert.notEqual(getComputedStyle(state()).pointerEvents, 'none');
    await app.activity(true);
    assert.ok(iframe() !== first, 'reactivation creates a new browsing context');
    assert.equal(state().dataset.variant, 'loading');
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.ok(!iframe(), 'disconnection releases its iframe');
    await act(async () => app.transport.setConnectionState('connected'));
    assert.ok(iframe());
    await app.replace(tree('https://example.com/second'));
    assert.equal(iframe().src, 'https://example.com/second');
    assert.equal(state().dataset.variant, 'loading');
    await act(async () => iframe().dispatchEvent(new window.Event('error')));
    assert.ok(!iframe(), 'failed frame releases its iframe'); assert.equal(state().dataset.variant, 'error');
    assert.notEqual(getComputedStyle(state()).pointerEvents, 'none');
    assert.equal(app.business().length, 0);
  } finally { await app.close(); }
});

for (const source of [{ src: 'https://example.com/wide' }, { srcDoc: '<p>Wide page</p>' }]) {
  test(`Iframe applies plugin minimum width to the ${Object.keys(source)[0]} document without width mode controls`, async () => {
    const tree = (minWidth) => ({
      component: 'Iframe',
      props: {
        ...source,
        title: 'Wide page',
        ...(minWidth === undefined ? {} : { minWidth }),
      },
    });
    const app = await setup(tree(undefined), { ...target, slot_id: 'conversation.panel.right' });
    try {
      let frame = app.container.querySelector('iframe');
      const viewport = app.container.querySelector('[data-testid="webui-iframe-viewport"]');
      assert.equal(getComputedStyle(viewport).overflow, 'auto');
      assert.equal(Number.parseFloat(getComputedStyle(frame).minWidth), 0);
      assert.equal(app.container.querySelector('[data-testid="webui-iframe-modes"]'), null);
      await app.replace(tree(720));
      frame = app.container.querySelector('iframe');
      assert.equal(getComputedStyle(frame).minWidth, '720px');
      app.container.style.width = '320px';
      assert.equal(
        app.container.querySelector('iframe'),
        frame,
        'resizing the container preserves the browsing context',
      );
      await app.replace(tree(0), 3);
      frame = app.container.querySelector('iframe');
      assert.equal(Number.parseFloat(getComputedStyle(frame).minWidth), 0);
      assert.equal(app.business().length, 0);
    } finally {
      await app.close();
    }
  });
}

test('Iframe reload retries the same URL and retains plugin minimum width and sandbox permissions', async () => {
  const app = await setup(
    {
      component: 'Iframe',
      props: {
        src: 'https://example.com/retry',
        title: 'Retry page',
        minWidth: 720,
      },
    },
    { ...target, slot_id: 'conversation.panel.right' },
  );
  try {
    const first = app.container.querySelector('iframe');
    await act(async () => first.dispatchEvent(new window.Event('load')));
    const source = app.container.querySelector('[data-testid="webui-iframe-source"]');
    assert.equal(source.textContent, 'https://example.com/retry');
    assert.equal(source.title, 'https://example.com/retry');
    await act(async () => app.container.querySelector('[data-testid="webui-iframe-reload"]').click());
    const reloaded = app.container.querySelector('iframe');
    assert.notEqual(reloaded, first);
    assert.equal(reloaded.src, first.src);
    assert.equal(reloaded.getAttribute('sandbox'), first.getAttribute('sandbox'));
    assert.equal(getComputedStyle(reloaded).minWidth, '720px');
    assert.equal(app.container.querySelector('[data-testid="webui-iframe-state"]').dataset.variant, 'loading');
    await act(async () => reloaded.dispatchEvent(new window.Event('error')));
    assert.equal(app.container.querySelector('iframe'), null);
    assert.equal(app.container.querySelector('[data-testid="webui-iframe-state"]').dataset.variant, 'error');
    await act(async () => app.container.querySelector('[data-testid="webui-iframe-reload"]').click());
    const retry = app.container.querySelector('iframe');
    assert.ok(retry);
    assert.equal(retry.src, first.src);
    assert.equal(retry.getAttribute('sandbox'), first.getAttribute('sandbox'));
    await act(async () => retry.dispatchEvent(new window.Event('load')));
    assert.equal(app.container.querySelector('[data-testid="webui-iframe-state"]'), null);
    const open = app.container.querySelector('[data-testid="webui-iframe-open-source"]');
    assert.equal(open.href, first.src);
    assert.equal(open.target, '_blank');
    assert.equal(open.rel, 'noopener noreferrer');
    assert.equal(app.business().length, 0);
  } finally {
    await app.close();
  }
});

test('Iframe slow loading stays interactive and cancels pending notices on completion and teardown', async (context) => {
  const timers = new Map();
  let nextTimer = -1;
  const originalSetTimeout = window.setTimeout.bind(window);
  const originalClearTimeout = window.clearTimeout.bind(window);
  context.mock.method(window, 'setTimeout', (callback, delay, ...args) => {
    if (delay !== 15000) return originalSetTimeout(callback, delay, ...args);
    const timer = nextTimer--;
    timers.set(timer, callback);
    return timer;
  });
  context.mock.method(window, 'clearTimeout', (timer) => {
    if (!timers.delete(timer)) originalClearTimeout(timer);
  });
  const tree = (src) => ({ component: 'Iframe', props: { src, title: 'Slow page' } });
  const app = await setup(tree('https://example.com/slow'), { ...target, slot_id: 'conversation.panel.right' });
  try {
    assert.equal(timers.size, 1);
    const frame = app.container.querySelector('iframe');
    await act(async () => {
      const [timer, callback] = [...timers][0];
      timers.delete(timer);
      callback();
    });
    const notice = app.container.querySelector('[data-testid="webui-iframe-state"]');
    assert.equal(notice.dataset.variant, 'slow');
    assert.equal(getComputedStyle(notice).pointerEvents, 'none');
    assert.equal(app.container.querySelector('iframe'), frame, 'slow loading must not discard the document');
    await act(async () => frame.dispatchEvent(new window.Event('load')));
    assert.equal(app.container.querySelector('[data-testid="webui-iframe-state"]'), null);
    await act(async () => app.container.querySelector('[data-testid="webui-iframe-reload"]').click());
    assert.equal(timers.size, 1);
    assert.equal(app.container.querySelector('[data-testid="webui-iframe-state"]').dataset.variant, 'loading');
    await act(async () => app.container.querySelector('iframe').dispatchEvent(new window.Event('load')));
    assert.equal(timers.size, 0, 'loaded frames cancel their slow-loading timer');
    await app.replace(tree('https://example.com/next'));
    assert.equal(timers.size, 1);
    await app.activity(false);
    assert.equal(timers.size, 0, 'inactive frames must not leave pending timers');
    await app.activity(true);
    assert.equal(timers.size, 1);
  } finally { await app.close(); }
  assert.equal(timers.size, 0, 'unmount cancels pending timers');
});

test('Iframe keeps relative and absolute host sources sandboxed without same-origin access', async () => {
  const tree = (src) => ({ component: 'Iframe', props: { src, title: 'Local preview' } });
  const app = await setup(tree('/preview'), { ...target, slot_id: 'conversation.panel.right' });
  try {
    for (const [revision, src] of [[2, '/preview'], [3, 'http://localhost/preview']]) {
      await app.replace(tree(src), revision);
      const iframe = app.container.querySelector('iframe');
      assert.ok(iframe);
      assert.equal(iframe.src, 'http://localhost/preview');
      const permissions = iframe.getAttribute('sandbox').split(/\s+/);
      assert.ok(permissions.includes('allow-scripts'));
      assert.ok(permissions.includes('allow-forms'));
      assert.ok(permissions.includes('allow-popups'));
      assert.ok(!permissions.includes('allow-same-origin'), 'host URLs must retain an opaque sandbox origin');
      assert.ok(!permissions.includes('allow-popups-to-escape-sandbox'));
      assert.ok(!permissions.some((permission) => permission.startsWith('allow-top-navigation')));
      assert.equal(iframe.hasAttribute('srcdoc'), false);
    }
  } finally { await app.close(); }
});

test('task navigation and its page mount without a session and retain their instances across chats', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const language = i18n.language;
  function Harness({ sessionId }) {
    const [page, setPage] = useState(null);
    return createElement(
      WebUIProvider,
      { runtime, registry: componentRendererRegistry },
      createElement(
        WebUISlotProvider,
        { sessionId, onOpenPage: setPage, activePage: page },
        createElement(WebUINavigationHost, { slotId: 'task.function' }),
        page ? createElement(WebUIPageHost, { target: page }) : null,
      ),
    );
  }
  const render = async (sessionId) => act(async () => root.render(createElement(Harness, { sessionId })));
  const taskTarget = {
    scope: 'application',
    slot_id: 'task.function',
    package_id: 'webui.workbench-v2',
    instance_key: 'task-inspector',
  };
  try {
    await act(async () => i18n.changeLanguage('en'));
    await render(null);
    const ready = transport.dispatches[0];
    assert.equal(ready.event, 'ui.ready');
    assert.equal(ready.target.scope, 'application');
    assert.equal(ready.target.sessionId, '');
    await act(async () =>
      transport.injectRenderFrame(
        frame(
          {
            component: 'Button',
            props: { value: { zh: '任务检查', en: 'Inspect tasks' } },
            events: { 'ui.click': { id: 'workbench.open' } },
          },
          1,
          taskTarget,
        ),
      ),
    );
    const entry = container.querySelector('[data-testid="webui-button"]');
    assert.equal(entry.textContent, 'Inspect tasks');
    await click(entry);
    const pageReady = transport.dispatches.find((request) => request.target.slotId === 'task.function.main');
    assert.ok(pageReady);
    assert.deepEqual(pageReady.target, {
      scope: 'application',
      sessionId: '',
      slotId: 'task.function.main',
      packageId: taskTarget.package_id,
      instanceKey: taskTarget.instance_key,
    });
    const clickRequest = transport.dispatches.find((request) => request.event === 'ui.click');
    assert.equal(clickRequest.target.scope, 'application');
    assert.equal(clickRequest.target.sessionId, '');
    await act(async () =>
      transport.injectRenderFrame(
        frame(
          {
            component: 'Iframe',
            props: { src: '/task-inspection', title: { zh: '任务检查页面', en: 'Task inspection page' } },
          },
          1,
          { ...taskTarget, slot_id: 'task.function.main' },
        ),
      ),
    );
    const page = container.querySelector('iframe');
    assert.ok(page);
    assert.equal(page.title, 'Task inspection page');
    for (const sessionId of ['session-a', 'session-b', null]) {
      await render(sessionId);
      assert.equal(container.querySelector('[data-testid="webui-button"]'), entry);
      assert.equal(container.querySelector('iframe'), page);
      assert.equal(transport.dispatches.filter((request) => request.event === 'ui.ready').length, 2);
    }
    await act(async () => i18n.changeLanguage('zh'));
    assert.equal(entry.textContent, '任务检查');
    assert.equal(page.title, '任务检查页面');
    assert.equal(runtime.getScopeSnapshot('', 'task.function').instanceKeys.length, 1);
    assert.equal(runtime.getScopeSnapshot('', 'task.function.main').instanceKeys.length, 1);
  } finally {
    await act(async () => root.unmount());
    container.remove();
    runtime.dispose();
    await act(async () => i18n.changeLanguage(language));
  }
});

test('WebUIEntryHost renders application.entry with Avatar and closes on clear frame', async () => {
  window.localStorage.removeItem('workswarm.webui.entry.visited');
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const entryTarget = {
    scope: 'application',
    slot_id: 'application.entry',
    package_id: 'webui.workbench-v2',
    instance_key: 'workbench',
  };
  try {
    await act(async () => {
      root.render(
        createElement(
          WebUIProvider,
          { runtime, registry: componentRendererRegistry },
          createElement(WebUIEntryHost, null, createElement('div', { 'data-testid': 'workspace-main' }, 'Workspace')),
        ),
      );
    });
    const ready = transport.dispatches.find((item) => item.event === 'ui.ready');
    assert.ok(ready, 'expected ui.ready on application.entry');
    assert.equal(ready.target.slotId, 'application.entry');
    assert.equal(ready.target.scope, 'application');
    assert.equal(ready.target.sessionId, '');
    assert.deepEqual(ready.payload, { reason: 'first_visit' });
    assert.equal(window.localStorage.getItem('workswarm.webui.entry.visited'), '1');

    await act(async () => {
      transport.injectRenderFrame({
        target: entryTarget,
        schema_version: 1,
        revision: 1,
        mode: 'replace',
        root: {
          component: 'Box',
          props: { direction: 'column', align: 'center' },
          children: [
            { component: 'Avatar', props: { src: '/logo.svg', alt: 'Welcome', size: 64 } },
            {
              component: 'Button',
              props: { value: 'Get started', type: 'primary' },
              events: { 'ui.click': { id: 'workbench.entry.close' } },
            },
          ],
        },
      });
    });

    const dialog = container.querySelector('[data-testid="webui-entry-host"]');
    assert.ok(dialog?.hasAttribute('open'), 'dialog opens when active tree arrives');
    const avatar = container.querySelector('[data-testid="webui-avatar"]');
    assert.ok(avatar, 'Avatar component renders in entry host');
    assert.equal(avatar.querySelector('img')?.getAttribute('src'), '/logo.svg');

    await click(container.querySelector('[data-testid="webui-button"]'));
    const clickDispatch = transport.dispatches.find((item) => item.event === 'ui.click');
    assert.ok(clickDispatch);
    assert.equal(clickDispatch.payload.action_id, 'workbench.entry.close');

    await act(async () => {
      transport.injectClearFrame({ target: entryTarget, revision: 2, reason: 'completed' });
    });
    assert.equal(dialog.hasAttribute('open'), false, 'clear closes the current view and keeps its consumer');
    await act(async () => transport.injectRenderFrame(frame({ component: 'Text', props: { value: 'Stale' } }, 1, entryTarget)));
    assert.equal(dialog.hasAttribute('open'), false, 'an old frame cannot reopen a cleared entry');
    await act(async () => transport.injectRenderFrame(frame({ component: 'Text', props: { value: 'Reopened' } }, 3, entryTarget)));
    assert.equal(dialog.hasAttribute('open'), true);
    assert.match(dialog.textContent, /Reopened/);
  } finally {
    await act(async () => root.unmount());
    container.remove();
    runtime.dispose();
  }
});


test('WebUIEntryHost keeps a consumer without repeating first-visit ready', async () => {
  window.localStorage.setItem('workswarm.webui.entry.visited', '1');
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(WebUIEntryHost,
      { runtime, registry: componentRendererRegistry }, createElement('span', null, 'Workspace'))));
    assert.equal(container.querySelector('[data-testid="webui-entry-host"]')?.open, false);
    assert.equal(container.textContent, 'Workspace');
    assert.equal(transport.dispatches.length, 0);
    const entryTarget = { scope: 'application', slot_id: 'application.entry', package_id: 'pkg.demo', instance_key: 'again' };
    await act(async () => transport.injectRenderFrame(frame({ component: 'Text', props: { value: 'Later view' } }, 1, entryTarget)));
    assert.equal(container.querySelector('[data-testid="webui-entry-host"]')?.open, true);
    assert.equal(transport.dispatches.length, 0);
    await act(async () => {
      transport.setConnectionState('disconnected');
      transport.setConnectionState('connected');
    });
    assert.equal(transport.dispatches.length, 0);
  } finally {
    await act(async () => root.unmount());
    container.remove(); runtime.dispose();
    window.localStorage.removeItem('workswarm.webui.entry.visited');
  }
});

const formTree = (rules = [{ required: true, message: '请输入账号' }, { min: 4, message: '至少四个字符' }]) => ({
  component: 'Box', props: { direction: 'column' }, children: [
    { component: 'Form', props: { name: 'credentials', requiredMark: false }, children: [
      { component: 'Form.Item', props: { name: 'account', label: '账号', rules }, children: [{ component: 'Input' }] },
      { component: 'Form.Item', props: { name: 'secret', label: '密码', rules: [{ pattern: '^[A-Za-z]+[0-9]+!$', message: '密码格式不正确' }] },
        children: [{ component: 'Input', props: { type: 'password' } }] },
    ] },
    { component: 'Button', props: { value: '确认', form: 'credentials' }, events },
    { component: 'Button', props: { value: '普通按钮' }, events },
  ],
});

test('Form validates on blur and click locally, then sends exactly its current fields', async () => {
  const app = await setup(formTree());
  try {
    assert.ok(app.container.querySelector('[data-testid="webui-form"]').classList.contains('ant-form-hide-required-mark'));
    const buttons = app.container.querySelectorAll('[data-testid="webui-button"]');
    await click(buttons[0]);
    await untilDOM(app.container, () => app.container.textContent.includes('请输入账号'));
    assert.equal(app.business().length, 0, 'hiding required markers preserves required validation');
    const fields = app.container.querySelectorAll('input');
    await changeInput(fields[0], 'abc');
    await act(async () => fields[0].dispatchEvent(new window.FocusEvent('focusout', { bubbles: true })));
    await untilDOM(app.container, () => app.container.textContent.includes('至少四个字符'));
    assert.equal(app.business().length, 0);
    assert.match(app.container.textContent, /至少四个字符/);
    assert.equal(fields[0].getAttribute('aria-invalid'), 'true');
    assert.ok(fields[0].getAttribute('aria-describedby'));
    await click(buttons[0]);
    assert.equal(app.business().length, 0);
    await changeInput(fields[0], 'alice');
    await changeInput(fields[1], 'invalid');
    await click(buttons[0]);
    assert.equal(app.business().length, 0);
    await untilDOM(app.container, () => app.container.textContent.includes('密码格式不正确'));
    await changeInput(fields[1], 'Secret123!');
    assert.equal(app.business().length, 0, 'typing never sends the secret');
    await click(buttons[0]);
    assert.deepEqual(app.business()[0].payload.data, { account: 'alice', secret: 'Secret123!' });
    assert.equal(app.business()[0].payload.action_id, 'confirm');
    await click(buttons[1]);
    assert.deepEqual(app.business()[1].payload.data, {});
  } finally { await app.close(); }
});

test('Form fields use literal names, optional rules, and stay isolated from another named Form', async () => {
  const tree = { component: 'Box', children: [
    { component: 'Form', props: { name: 'one' }, children: [
      { component: 'Form.Item', props: { name: 'literal.name' }, children: [{ component: 'Input' }] },
    ] },
    { component: 'Form', props: { name: 'two' }, children: [
      { component: 'Form.Item', props: { name: 'other', rules: [{ required: true, message: 'Required' }] }, children: [{ component: 'Input' }] },
    ] },
    { component: 'Button', props: { value: 'First', form: 'one' }, events },
  ] };
  const app = await setup(tree);
  try {
    await click(app.container.querySelector('[data-testid="webui-button"]'));
    assert.deepEqual(app.business()[0].payload.data, { 'literal.name': '' });
    await changeInput(app.container.querySelector('input'), '值');
    await click(app.container.querySelector('[data-testid="webui-button"]'));
    assert.deepEqual(app.business()[1].payload.data, { 'literal.name': '值' });
    await act(async () => app.transport.setConnectionState('disconnected'));
    assert.equal(app.container.querySelector('input').value, '');
    assert.equal(app.container.querySelector('input').disabled, true);
  } finally { await app.close(); }
});

test('Dropdown expands locally and sends only the registered clicked menu key', async () => {
  const app = await setup({ component: 'Dropdown', props: { items: [
    { key: 'account', label: 'alice', disabled: true }, { type: 'divider' }, { key: 'exit', label: '退出登录' },
  ],
    placement: 'topLeft', align: { points: ['bl', 'tr'], offset: [0, 0] } }, events: { 'ui.select': events['ui.click'] },
    children: [{ component: 'Avatar', props: { src: '/logo.svg', alt: 'User' } }] });
  try {
    await click(app.container.querySelector('[data-testid="webui-dropdown-trigger"]'));
    assert.equal(app.business().length, 0);
    const account = document.querySelector('[data-testid="webui-dropdown-item"][data-variant="account"]');
    assert.equal(account.closest('[role="menuitem"]').getAttribute('aria-disabled'), 'true');
    assert.equal(document.querySelectorAll('[role="separator"]').length, 1);
    await click(account);
    assert.equal(app.business().length, 0, 'the native disabled label cannot dispatch an action');
    const item = document.querySelector('[data-testid="webui-dropdown-item"][data-variant="exit"]');
    assert.equal(item.textContent, '退出登录');
    await click(item);
    assert.equal(app.business()[0].event, 'ui.select');
    assert.deepEqual(app.business()[0].payload.data, { key: 'exit' });
    assert.equal(app.business()[0].payload.action_id, 'confirm');
  } finally { await app.close(); }
});

test('Dropdown renders plugin-composed labels and native popup width without initial action focus', async () => {
  const app = await setup({ component: 'Dropdown', props: {
    autoFocus: false, destroyOnHidden: true, styles: { root: { width: 200 } }, items: [
      { key: 'account', disabled: true, label: [{ component: 'Box', props: { align: 'center', gap: 'sm' }, children: [
        { component: 'Avatar', props: { children: 'A', alt: 'User', size: 20, style: { backgroundColor: '#B1E795', color: '#FFFFFF', fontSize: 10 } } },
        { component: 'Text', props: { value: 'Alice' } },
      ] }] },
      { type: 'divider' }, { key: 'exit', label: 'Sign out' },
    ],
  }, events: { 'ui.select': events['ui.click'] }, children: [{ component: 'Avatar', props: { children: 'A', alt: 'User', style: { backgroundColor: '#B1E795', color: '#FFFFFF', fontSize: 14 } } }] });
  try {
    const trigger = app.container.querySelector('[data-testid="webui-dropdown-trigger"]');
    trigger.focus();
    await click(trigger);
    const account = document.querySelector('[data-testid="webui-dropdown-item"][data-variant="account"]');
    assert.equal(account.querySelector('[data-testid="webui-avatar"]').textContent, 'A');
    assert.equal(account.querySelector('[data-testid="webui-avatar"]').style.backgroundColor,
      trigger.querySelector('[data-testid="webui-avatar"]').style.backgroundColor);
    assert.equal(account.querySelector('[data-testid="webui-avatar"]').style.width, '20px');
    assert.equal(account.querySelector('[data-testid="webui-text"]').textContent, 'Alice');
    assert.equal(document.querySelector('.ant-dropdown').style.width, '200px');
    assert.equal(document.activeElement, trigger);
    assert.equal(document.querySelector('.ant-dropdown-menu-item-active'), null);
    await click(account);
    assert.equal(app.business().length, 0, 'display labels do not dispatch actions');
    const menu = document.querySelector('[role="menu"]');
    await click(trigger);
    await untilDOM(document.body, () => !document.querySelector('[role="menu"]'));
    await click(trigger);
    assert.notEqual(document.querySelector('[role="menu"]'), menu, 'native destroyOnHidden recreates the popup');
    await click(document.querySelector('[data-testid="webui-dropdown-item"][data-variant="exit"]'));
    assert.deepEqual(app.business()[0].payload.data, { key: 'exit' });
  } finally { await app.close(); }
});

test('application footer mounts without a session and follows the published single-slot semantics', async () => {
  const scope = { scope: 'application', slot_id: 'application.sidebar.footer', package_id: 'pkg.one', instance_key: 'main' };
  const app = await setup({ component: 'Text', props: { value: 'First' } }, scope);
  try {
    await act(async () => app.transport.injectRenderFrame(frame({ component: 'Text', props: { value: 'Second' } }, 1,
      { ...scope, package_id: 'pkg.two' })));
    assert.doesNotMatch(app.container.textContent, /First/);
    assert.match(app.container.textContent, /Second/);
    assert.equal(app.transport.dispatches[0].target.scope, 'application');
    assert.equal(app.transport.dispatches[0].target.sessionId, '');
  } finally { await app.close(); }
});

const backgroundScope = {
  scope: 'application', slot_id: 'application.background', package_id: 'pkg.theme', instance_key: 'main',
};
const surfaceAsset = (name) => `/extension-assets/pkg.theme/g1/${name}`;
const surface = (props) => ({ component: 'MediaSurface', props: { title: 'Theme', ...props } });
const surfaceToken = (iframe) => /#webui-surface-token=([0-9a-f]{32})$/.exec(iframe.getAttribute('src'))?.[1];
async function postSurface(source, data) {
  await act(async () => window.dispatchEvent(new window.MessageEvent('message', { data, source })));
}
const surfaceMessage = (token, event) => ({ type: 'webui.surface', version: 1, token, event });

test('MediaSurface renders image, video and sandboxed pages with the host media policy', async () => {
  const app = await setup(surface({ kind: 'image', src: surfaceAsset('bg.png') }), backgroundScope);
  try {
    const root = app.container.querySelector('[data-testid="webui-media-surface"]');
    assert.equal(root.dataset.variant, 'image');
    assert.equal(root.getAttribute('aria-hidden'), 'true');
    assert.equal(root.style.opacity, '1');
    const image = root.querySelector('[data-testid="webui-media-surface-image"]');
    assert.equal(image.getAttribute('src'), surfaceAsset('bg.png'));
    assert.equal(image.style.objectFit, 'cover');
    assert.equal(image.style.objectPosition, 'center');
    assert.equal(root.querySelector('[data-testid="webui-media-surface-dim"]'), null);

    await app.replace(surface({ kind: 'video', src: surfaceAsset('loop.mp4'), poster: surfaceAsset('poster.jpg'),
      fit: 'contain', position: 'top', loop: false, muted: false, dim: 0.4, blur: 10, opacity: 0.5 }), 2);
    const video = app.container.querySelector('[data-testid="webui-media-surface-video"]');
    assert.equal(video.muted, true, 'the background ignores muted=false');
    assert.equal(video.loop, false);
    assert.equal(video.autoplay, true);
    assert.equal(video.hasAttribute('playsinline'), true);
    assert.equal(video.hasAttribute('controls'), false);
    assert.equal(video.getAttribute('poster'), surfaceAsset('poster.jpg'));
    assert.equal(video.style.objectFit, 'contain');
    assert.equal(video.style.objectPosition, 'top');
    const layer = video.parentElement;
    assert.equal(layer.style.filter, 'blur(10px)');
    // Blurred media is scaled past the edges (2 × blur on the shortest side) instead of being enlarged.
    assert.equal(layer.style.inset, '');
    assert.equal(layer.style.transform, `scale(${(1 + 40 / Math.min(window.innerWidth, window.innerHeight)).toFixed(4)})`);
    assert.equal(app.container.querySelector('[data-testid="webui-media-surface-dim"]').style.opacity, '0.4');
    assert.equal(app.container.querySelector('[data-testid="webui-media-surface"]').style.opacity, '0.5');

    await app.replace(surface({ kind: 'html', src: surfaceAsset('scene/index.html#scene=1'),
      poster: surfaceAsset('poster.jpg') }), 3);
    const iframe = app.container.querySelector('[data-testid="webui-media-surface-frame"]');
    assert.equal(iframe.getAttribute('sandbox'), 'allow-scripts');
    assert.equal(iframe.getAttribute('allow'), '', 'a background page gets no autoplay delegation, so it cannot start sound');
    assert.equal(iframe.getAttribute('referrerpolicy'), 'no-referrer');
    assert.equal(iframe.getAttribute('tabindex'), '-1');
    assert.equal(iframe.getAttribute('title'), 'Theme');
    assert.match(iframe.getAttribute('src'), /^\/extension-assets\/pkg\.theme\/g1\/scene\/index\.html#scene=1&webui-surface-token=[0-9a-f]{32}$/);
    assert.equal(iframe.dataset.variant, 'loading');
    assert.equal(iframe.style.opacity, '0', 'the page keeps rendering while transparent');
    assert.ok(app.container.querySelector('[data-testid="webui-media-surface-poster"]'));
  } finally { await app.close(); }
});

test('MediaSurface accepts only messages from its frame that carry the current token', async (context) => {
  const app = await setup(surface({ kind: 'html', src: surfaceAsset('scene.html'), poster: surfaceAsset('poster.jpg') }),
    backgroundScope);
  try {
    const iframe = app.container.querySelector('[data-testid="webui-media-surface-frame"]');
    const frameWindow = iframe.contentWindow;
    const token = surfaceToken(iframe);
    assert.ok(frameWindow && token);
    const posted = [];
    context.mock.method(frameWindow, 'postMessage', (message, targetOrigin) => posted.push({ message, targetOrigin }));
    for (const [source, data] of [
      [frameWindow, surfaceMessage('0'.repeat(32), 'ready')],
      [window, surfaceMessage(token, 'ready')],
      [null, surfaceMessage(token, 'ready')],
      [frameWindow, { ...surfaceMessage(token, 'ready'), version: 2 }],
      [frameWindow, { ...surfaceMessage(token, 'ready'), type: 'webui.other' }],
      [frameWindow, surfaceMessage(token, 'play')],
      [frameWindow, surfaceMessage(token, 'heartbeat')],
      [frameWindow, surfaceMessage(token, 'complete')],
      [frameWindow, 'ready'],
    ]) await postSurface(source, data);
    assert.equal(iframe.dataset.variant, 'loading');
    assert.equal(posted.length, 0);

    await postSurface(frameWindow, surfaceMessage(token, 'ready'));
    assert.equal(iframe.dataset.variant, 'ready');
    assert.equal(iframe.style.opacity, '1');
    assert.equal(app.container.querySelector('[data-testid="webui-media-surface-poster"]'), null);
    assert.deepEqual(posted.at(-1), { targetOrigin: '*', message: { type: 'webui.surface', version: 1, event: 'start',
      context: { role: 'background', visible: true, reducedMotion: false, locale: i18n.language } } });

    await postSurface(frameWindow, surfaceMessage(token, 'error'));
    assert.equal(app.container.querySelector('[data-testid="webui-media-surface-frame"]'), null);
    assert.ok(app.container.querySelector('[data-testid="webui-media-surface-poster"]'));

    await app.replace(surface({ kind: 'html', src: surfaceAsset('next.html') }), 2);
    const next = app.container.querySelector('[data-testid="webui-media-surface-frame"]');
    assert.notEqual(surfaceToken(next), token, 'each iframe receives a fresh token');
    assert.equal(iframe.isConnected, false, 'switching removes the previous page right away, so its audio stops');
    await postSurface(next.contentWindow, surfaceMessage(token, 'ready'));
    assert.equal(next.dataset.variant, 'loading');
    await postSurface(next.contentWindow, surfaceMessage(surfaceToken(next), 'ready'));
    assert.equal(next.dataset.variant, 'ready');
  } finally { await app.close(); }
});

test('MediaSurface falls back to the poster after the ready timeout and keeps videos paused while hidden', async (context) => {
  const timers = [];
  const originalSetTimeout = window.setTimeout.bind(window);
  context.mock.method(window, 'setTimeout', (callback, delay, ...args) => {
    if (delay !== 15000) return originalSetTimeout(callback, delay, ...args);
    timers.push(callback);
    return -timers.length;
  });
  const app = await setup(surface({ kind: 'html', src: surfaceAsset('slow.html'), poster: surfaceAsset('poster.jpg') }),
    backgroundScope);
  const visibility = Object.getOwnPropertyDescriptor(document, 'visibilityState');
  try {
    assert.equal(timers.length, 1);
    await act(async () => timers[0]());
    assert.equal(app.container.querySelector('[data-testid="webui-media-surface-frame"]'), null);
    assert.ok(app.container.querySelector('[data-testid="webui-media-surface-poster"]'));

    await app.replace(surface({ kind: 'video', src: surfaceAsset('loop.mp4') }), 2);
    const video = app.container.querySelector('[data-testid="webui-media-surface-video"]');
    const paused = context.mock.method(video, 'pause', () => undefined);
    const played = context.mock.method(video, 'play', () => Promise.resolve());
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'hidden' });
    await act(async () => document.dispatchEvent(new window.Event('visibilitychange')));
    assert.equal(paused.mock.callCount(), 1);
    Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => 'visible' });
    await act(async () => document.dispatchEvent(new window.Event('visibilitychange')));
    assert.equal(played.mock.callCount(), 1);
  } finally {
    if (visibility) Object.defineProperty(document, 'visibilityState', visibility);
    else delete document.visibilityState;
    await app.close();
  }
});

test('MediaSurface shows only the poster under reduced motion when one is provided', async () => {
  const matchMedia = window.matchMedia;
  window.matchMedia = (query) => ({ ...matchMedia(query), matches: query === '(prefers-reduced-motion: reduce)' });
  const app = await setup(surface({ kind: 'video', src: surfaceAsset('loop.mp4'), poster: surfaceAsset('poster.jpg') }),
    backgroundScope);
  try {
    assert.equal(app.container.querySelector('[data-testid="webui-media-surface-video"]'), null);
    assert.ok(app.container.querySelector('[data-testid="webui-media-surface-poster"]'));
    await app.replace(surface({ kind: 'html', src: surfaceAsset('scene.html'), poster: surfaceAsset('poster.jpg') }), 2);
    assert.equal(app.container.querySelector('[data-testid="webui-media-surface-frame"]'), null);
    await app.replace(surface({ kind: 'video', src: surfaceAsset('loop.mp4') }), 3);
    const video = app.container.querySelector('[data-testid="webui-media-surface-video"]');
    assert.equal(video.autoplay, false, 'without a poster the video stays on its first frame');
    await app.replace(surface({ kind: 'html', src: surfaceAsset('scene.html') }), 4);
    assert.ok(app.container.querySelector('[data-testid="webui-media-surface-frame"]'));
  } finally {
    window.matchMedia = matchMedia;
    await app.close();
  }
});

test('WebUIBackgroundHost mounts application.background below the UI and marks the document while shown', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  const marker = () => document.documentElement.getAttribute('data-webui-background');
  try {
    await act(async () => root.render(createElement(WebUIBackgroundHost, { runtime, registry: componentRendererRegistry })));
    const host = container.querySelector('[data-testid="webui-background-host"]');
    assert.equal(host.hasAttribute('inert'), true);
    assert.equal(host.getAttribute('aria-hidden'), 'true');
    assert.equal(marker(), null);
    const ready = transport.dispatches.find((request) => request.event === 'ui.ready');
    assert.equal(ready.target.slotId, 'application.background');
    assert.equal(ready.target.scope, 'application');

    await act(async () => transport.injectRenderFrame(frame(surface({ kind: 'image', src: surfaceAsset('bg.png') }), 1,
      backgroundScope)));
    assert.ok(host.querySelector('[data-testid="webui-media-surface"]'));
    assert.equal(marker(), 'active');

    await act(async () => transport.injectRenderFrame(frame({ component: 'Box', props: {} }, 2, backgroundScope)));
    assert.ok(host.querySelector('[data-testid="webui-media-surface"]'), 'an invalid root keeps the last valid frame');

    await act(async () => transport.injectClearFrame({ target: backgroundScope, revision: 3 }));
    assert.equal(host.querySelector('[data-testid="webui-media-surface"]'), null);
    assert.equal(marker(), null);
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    container.remove();
    assert.equal(marker(), null);
  }
});

test('background updates with the same page keep it running and only restyle it', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  const changes = [];
  const observer = new MutationObserver(() => changes.push(document.documentElement.getAttribute('data-webui-background')));
  try {
    await act(async () => root.render(createElement(WebUIBackgroundHost, { runtime, registry: componentRendererRegistry })));
    const page = surfaceAsset('scene/index.html');
    await act(async () => transport.injectRenderFrame(frame(surface({ kind: 'html', src: page, dim: 0.1 }), 1, backgroundScope)));
    const first = container.querySelector('[data-testid="webui-media-surface-frame"]');
    assert.ok(first);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: ['data-webui-background'] });

    // A slider drag re-renders the same page with new dim and blur values.
    for (const [revision, dim, blur] of [[2, 0.2, 0], [3, 0.35, 4], [4, 0.5, 8]]) {
      await act(async () => transport.injectRenderFrame(frame(surface({ kind: 'html', src: page, dim, blur }), revision,
        backgroundScope)));
    }
    const current = container.querySelector('[data-testid="webui-media-surface-frame"]');
    assert.equal(current === first, true, 'the running page is not reloaded');
    assert.equal(container.querySelector('[data-testid="webui-media-surface-dim"]').style.opacity, '0.5');
    const layer = container.querySelector('.webui-media-surface__layer');
    assert.equal(layer.style.filter, 'blur(8px)');
    assert.equal(layer.style.inset, '', 'blur never changes the size the page is laid out in');
    assert.deepEqual(changes, [], 'the document stays marked while the background updates');

    await act(async () => transport.injectRenderFrame(frame(surface({ kind: 'html', src: surfaceAsset('other/index.html') }), 5,
      backgroundScope)));
    assert.equal(container.querySelector('[data-testid="webui-media-surface-frame"]') === first, false, 'a new page loads anew');
  } finally {
    observer.disconnect();
    await act(async () => root.unmount());
    runtime.dispose();
    container.remove();
  }
});

test('a new request context can replace the blocking entry without exposing the old view', async () => {
  const target = { scope: 'application', slot_id: 'application.entry', package_id: 'pkg.demo', instance_key: 'old' };
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog }); runtime.start();
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(WebUIEntryHost, { runtime, registry: componentRendererRegistry })));
    await act(async () => transport.injectRenderFrame(frame({ component: 'Text', props: { value: 'Previous view' } }, 5, target)));
    await act(async () => runtime.resetRequestContext());
    assert.equal(container.querySelector('dialog').open, true);
    const next = { ...target, instance_key: 'new' };
    await act(async () => transport.injectRenderFrame(frame({ component: 'Button', props: { value: 'Current view' }, events }, 1, next)));
    assert.doesNotMatch(container.textContent, /Previous view/);
    await click(container.querySelector('[data-testid="webui-button"]'));
    assert.equal(transport.dispatches.findLast((item) => item.event === 'ui.click').target.instanceKey, 'new');
    await act(async () => transport.injectClearFrame({ target: next, revision: 2 }));
    assert.equal(container.querySelector('dialog').open, false);
  } finally { await act(async () => root.unmount()); container.remove(); runtime.dispose(); }
});


test('FileTree retains scrolling across directory expansion and preview data frames', async () => {
  const directory = (loaded) => ({
    key: 'dir', title: 'Directory', path: 'dir', kind: 'directory', loaded,
    ...(loaded ? { children: [{ key: 'child', title: 'Child', path: 'dir/child.txt', kind: 'file' }] } : {}),
  });
  const tree = (loaded, content = null) => ({
    component: 'Panel', props: { title: 'Files' }, children: [{
      component: 'Box', props: { direction: 'row' }, children: [
        { component: 'FileTree', props: { treeData: [directory(loaded)] }, events: {
          'ui.change': { id: 'files.expand' }, 'ui.select': { id: 'files.select' },
        } },
        { component: 'FilePreview', props: { file: content === null ? null : {
          name: 'child.txt', path: 'dir/child.txt', content,
        } } },
      ],
    }],
  });
  const app = await setup(tree(false), { ...target, slot_id: 'conversation.panel.right' });
  const body = () => app.container.querySelector('[data-testid="webui-file-tree-body"]');
  try {
    const original = body();
    original.scrollTop = 320;
    await act(async () => original.dispatchEvent(new window.Event('scroll')));
    await click(app.container.querySelector('[data-variant="dir"][data-testid="webui-file-tree-item"]'));
    assert.equal(body().scrollTop, 320);
    assert.deepEqual(app.business()[0].payload.data.expandedKeys, ['dir']);
    await app.replace(tree(true));
    assert.notEqual(body(), original, 'the existing frame remount behavior is unchanged');
    assert.equal(body().scrollTop, 320);
    await click(app.container.querySelector('[data-variant="child"][data-testid="webui-file-tree-item"]'));
    await app.replace(tree(true, 'selected child'), 3);
    assert.equal(body().scrollTop, 320);
    assert.equal(app.container.querySelector('[data-testid="webui-file-preview-text"]').textContent, 'selected child');
    assert.ok(app.container.querySelector('.ant-tree-node-selected [data-variant="child"]'));
  } finally { await app.close(); }
});

test('FileTree resizes with pointer and keyboard, retains width on replace and resets on clear', async () => {
  const tree = { component: 'FileTree', props: { treeData: [
    { key: 'file', title: 'File', path: 'file.txt', kind: 'file' },
  ] } };
  const app = await setup(tree);
  const section = () => app.container.querySelector('[data-testid="webui-file-tree"]');
  const separator = () => app.container.querySelector('[data-testid="webui-file-tree-resize"]');
  const pointer = async (type, x, pointerId = 7) => {
    const event = new window.MouseEvent(type, { bubbles: true, button: 0, clientX: x });
    Object.defineProperty(event, 'pointerId', { value: pointerId });
    await act(async () => separator().dispatchEvent(event));
  };
  try {
    section().parentElement.getBoundingClientRect = () => ({ width: 600 });
    section().getBoundingClientRect = () => ({ width: parseFloat(section().style.flexBasis) });
    let capture = null;
    separator().setPointerCapture = (id) => { capture = id; };
    separator().releasePointerCapture = (id) => { assert.equal(capture, id); capture = null; };
    await pointer('pointerdown', 240);
    assert.equal(capture, 7);
    assert.equal(document.activeElement, separator());
    await pointer('pointermove', 340, 8);
    assert.equal(section().style.flexBasis, '240px');
    await pointer('pointermove', 340);
    assert.equal(section().style.flexBasis, '340px');
    await pointer('pointermove', 2000);
    assert.equal(section().style.flexBasis, '440px', 'the preview keeps 160px');
    await pointer('pointermove', -2000);
    assert.equal(section().style.flexBasis, '140px');
    await pointer('pointerup', -2000);
    assert.equal(capture, null);
    await pointer('pointermove', 400);
    assert.equal(section().style.flexBasis, '140px');
    await pressKey(separator(), 'ArrowRight', 39);
    assert.equal(section().style.flexBasis, '164px');
    assert.equal(separator().getAttribute('aria-valuenow'), '164');
    await app.replace(tree);
    assert.equal(section().style.flexBasis, '164px');
    assert.equal(app.business().length, 0, 'resizing sends no plugin actions');
    await act(async () => app.transport.injectClearFrame({ target, revision: 3, reason: 'completed' }));
    await app.replace(tree, 4);
    assert.equal(section().style.flexBasis, '240px');
    assert.equal(app.container.querySelector('[data-testid="webui-file-tree-body"]').scrollTop, 0);
  } finally { await app.close(); }
});

test('FileTree viewport state is isolated by node and discarded when the node is removed', async () => {
  const fileTree = { component: 'FileTree', props: { treeData: [
    { key: 'file', title: 'File', path: 'file.txt', kind: 'file' },
  ] } };
  const tree = { component: 'Box', children: [fileTree, fileTree] };
  const app = await setup(tree);
  const bodies = () => app.container.querySelectorAll('[data-testid="webui-file-tree-body"]');
  try {
    bodies()[0].scrollTop = 260;
    await act(async () => bodies()[0].dispatchEvent(new window.Event('scroll')));
    await app.replace(tree);
    assert.equal(bodies()[0].scrollTop, 260);
    assert.equal(bodies()[1].scrollTop, 0);
    await app.replace({ component: 'Box', children: [{ component: 'Text', props: { value: 'No files' } }, fileTree] }, 3);
    await app.replace(tree, 4);
    assert.equal(bodies()[0].scrollTop, 0);
  } finally { await app.close(); }
});

for (const operation of ['expand', 'select']) {
  test(`FileTree rejected ${operation} preserves independent state updates`, async () => {
    const selection = createComponentState({ selectedKey: null, expandedKeys: [] });
    const treeData = [
      { key: 'dir', title: 'Directory', path: 'dir', kind: 'directory', loaded: operation === 'select' },
      { key: 'file', title: 'File', path: 'file.txt', kind: 'file' },
    ];
    let settle;
    const request = () => new Promise((resolve) => { settle = resolve; });
    const container = document.createElement('div'); document.body.append(container);
    const root = createRoot(container);
    try {
      await act(async () => root.render(createElement(FileTree, {
        treeData, selection, onSelect: request, onExpand: request,
      })));
      const key = operation === 'expand' ? 'dir' : 'file';
      await click(container.querySelector(`[data-testid="webui-file-tree-item"][data-variant="${key}"]`));
      assert.equal(typeof settle, 'function');
      await act(async () => {
        selection.set(operation === 'expand'
          ? { ...selection.getSnapshot(), selectedKey: 'file' }
          : { ...selection.getSnapshot(), expandedKeys: ['dir'] });
      });
      await act(async () => settle({ status: 'sent', receipt: { accepted: false } }));
      assert.deepEqual(selection.getSnapshot(), operation === 'expand'
        ? { selectedKey: 'file', expandedKeys: [] } : { selectedKey: null, expandedKeys: ['dir'] });
    } finally {
      await act(async () => root.unmount()); container.remove();
    }
  });
}

test('Dropdown with a non-preview Image trigger opens only its menu', async () => {
  const app = await setup({ component: 'Dropdown', props: { items: [{ key: 'one', label: 'One' }] }, events: { 'ui.select': events['ui.click'] },
    children: [{ component: 'Box', children: [
      { component: 'Image', props: { src: '/logo.svg', alt: 'Menu trigger', preview: false } },
    ] }] });
  try {
    await click(app.container.querySelector('img'));
    await untilDOM(document.body, () => Boolean(document.querySelector('[role="menu"]')));
    assert.equal(document.querySelector('.ant-image-preview'), null);
    assert.equal(app.business().length, 0);
    await click(document.querySelector('[data-testid="webui-dropdown-item"]'));
    assert.equal(app.business().length, 1);
    assert.deepEqual(app.business()[0].payload.data, { key: 'one' });
  } finally { await app.close(); }
});

test('Form drafts survive retained Tabs and activity changes but reset on disconnect or destroyed content', async () => {
  for (const destroyOnHidden of [false, true]) {
    const tree = { component: 'Tabs', props: { destroyOnHidden, items: [
      { key: 'form', label: 'Form', children: [formTree([])] },
      { key: 'other', label: 'Other', children: [{ component: 'Text', props: { value: 'Other content' } }] },
    ] } };
    const app = await setup(tree);
    try {
      const input = () => app.container.querySelector('input');
      await changeInput(input(), 'keep-this-draft');
      await click(app.container.querySelector('[data-testid="webui-tab-label"][data-variant="other"]'));
      assert.equal(app.business().length, 0);
      await click(app.container.querySelector('[data-testid="webui-tab-label"][data-variant="form"]'));
      assert.equal(input().value, destroyOnHidden ? '' : 'keep-this-draft');
      await changeInput(input(), 'local-draft');
      await app.activity(false);
      await app.activity(true);
      assert.equal(input().value, 'local-draft');
      await act(async () => app.transport.setConnectionState('disconnected'));
      await act(async () => app.transport.setConnectionState('connected'));
      assert.equal(input().value, '');
    } finally { await app.close(); }
  }
});

test('a plugin display preference hides bars and detail tabs across conversations without losing frames', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const packageId = 'settings-visibility-test';
  const binding = { key: 'visible', default: true };
  const widget = { component: 'Box', children: [{ component: 'Text', props: { value: 'Observed usage' } }] };
  const setting = { component: 'Switch', props: { preference: binding, ariaLabel: 'Show usage' } };
  const settingTarget = {
    session_id: '',
    scope: 'application',
    slot_id: 'application.settings',
    package_id: packageId,
    instance_key: 'show',
  };
  const targetFor = (session) => ({ ...target, session_id: session, package_id: packageId });
  const panelTarget = (session, owner = packageId) => ({
    ...targetFor(session),
    slot_id: 'conversation.panel.right',
    package_id: owner,
    instance_key: 'details',
  });
  const details = (value) => ({
    component: 'Iframe',
    props: {
      title: 'Usage details',
      srcDoc: `<p>${value}</p>`,
    },
  });
  const visibleFrame = (tree, revision, owner) => ({ ...frame(tree, revision, owner), visible_when: binding });
  function Panels({ sessionId }) {
    const { panels, renderPanel } = useWebUIPanels({ slotId: 'conversation.panel.right', sessionId });
    return createElement(
      'section',
      null,
      ...panels.map((panel) =>
        createElement(
          'div',
          { key: panel.key },
          createElement('button', { 'data-testid': 'webui-test-panel-tab' }, panel.title),
          renderPanel(panel.key),
        ),
      ),
    );
  }
  const tabs = () => container.querySelectorAll('[data-testid="webui-test-panel-tab"]');
  const iframes = () => container.querySelectorAll('iframe');
  const keysBefore = new Set(Object.keys(window.localStorage));
  try {
    await act(async () =>
      root.render(
        createElement(
          WebUIProvider,
          { runtime, registry: componentRendererRegistry },
          createElement(WebUIHost, { slotId: target.slot_id, sessionId: 's1' }),
          createElement(WebUIHost, { slotId: target.slot_id, sessionId: 's2' }),
          createElement(Panels, { sessionId: 's1' }),
          createElement(Panels, { sessionId: 's2' }),
          createElement(WebUIHost, {
            slotId: settingTarget.slot_id,
            scope: 'application',
            packageId,
            instanceKey: 'show',
          }),
        ),
      ),
    );
    await act(async () => {
      transport.injectRenderFrame(visibleFrame(widget, 1, targetFor('s1')));
      transport.injectRenderFrame(visibleFrame(widget, 1, targetFor('s2')));
      transport.injectRenderFrame(visibleFrame(details('Observed history s1'), 1, panelTarget('s1')));
      transport.injectRenderFrame(visibleFrame(details('Observed history s2'), 1, panelTarget('s2')));
      transport.injectRenderFrame(visibleFrame(details('Another package'), 1, panelTarget('s1', 'unrelated-package')));
      transport.injectRenderFrame(frame(setting, 1, settingTarget));
    });
    assert.equal(container.querySelectorAll('[data-variant="conversation.input.above"]').length, 2);
    assert.equal(tabs().length, 3);
    assert.equal(iframes().length, 3);
    const originalDocument = iframes()[0];
    const control = () => container.querySelector('[data-testid="webui-switch"]');
    await click(control());
    assert.equal(control().getAttribute('aria-checked'), 'false');
    assert.equal(container.querySelectorAll('[data-variant="conversation.input.above"]').length, 0);
    assert.equal(tabs().length, 1, 'the unrelated package keeps its tab');
    assert.equal(iframes().length, 1, 'hidden documents release their browsing contexts');
    assert.equal(originalDocument.isConnected, false);
    await act(async () => {
      transport.injectRenderFrame(visibleFrame(widget, 2, targetFor('s1')));
      transport.injectRenderFrame(visibleFrame(details('Updated history while hidden'), 2, panelTarget('s1')));
      transport.injectRenderFrame(frame(setting, 2, settingTarget));
    });
    assert.equal(control().getAttribute('aria-checked'), 'false');
    assert.equal(container.querySelectorAll('[data-variant="conversation.input.above"]').length, 0);
    assert.equal(tabs().length, 1, 'background replacements cannot reveal a hidden tab');
    assert.equal(
      runtime.getScopeSnapshot('s1', 'conversation.panel.right').instanceKeys.length,
      2,
      'hidden instances retain their runtime state',
    );
    await click(control());
    assert.equal(container.querySelectorAll('[data-variant="conversation.input.above"]').length, 2);
    assert.equal(tabs().length, 3);
    assert.equal(iframes().length, 3);
    assert.ok([...iframes()].some((node) => node.getAttribute('srcdoc').includes('Updated history while hidden')));
    assert.equal(panelNavigation(runtime).getSnapshot(), null, 'enabling does not request panel selection');
    const storageKey = Object.keys(window.localStorage).find((key) => !keysBefore.has(key));
    window.localStorage.setItem(storageKey, 'false');
    await act(async () =>
      window.dispatchEvent(
        new window.StorageEvent('storage', {
          key: storageKey,
          newValue: 'false',
          storageArea: window.localStorage,
        }),
      ),
    );
    assert.equal(control().getAttribute('aria-checked'), 'false');
    assert.equal(container.querySelectorAll('[data-variant="conversation.input.above"]').length, 0);
    assert.equal(tabs().length, 1, 'storage updates apply to detail tabs too');
    const source = { sessionId: 's1', slotId: target.slot_id, packageId, instanceKey: target.instance_key };
    assert.equal(requestPanelOpen(runtime, source, 'details'), false, 'hidden instances cannot request navigation');
    await act(async () =>
      transport.injectRenderFrame({
        ...visibleFrame(widget, 3, targetFor('s1')),
        visible_when: { key: 'another', default: true },
      }),
    );
    assert.equal(
      container.querySelectorAll('[data-variant="conversation.input.above"]').length,
      1,
      'changing only instance metadata invalidates the host scope',
    );
    assert.equal(requestPanelOpen(runtime, source, 'details'), false, 'a hidden target cannot be opened');
    await act(async () => transport.injectRenderFrame(frame(widget, 4, targetFor('s1'))));
    assert.equal(
      container.querySelectorAll('[data-variant="conversation.input.above"]').length,
      1,
      'omitting the condition removes it instead of retaining the prior binding',
    );
    const nestedWidget = { component: 'Box', children: [widget] };
    await act(async () => transport.injectRenderFrame(visibleFrame(nestedWidget, 5, targetFor('s1'))));
    assert.equal(
      container.querySelectorAll('[data-variant="conversation.input.above"]').length,
      0,
      'instance visibility hides the container regardless of the component tree layout',
    );
    await act(async () => transport.injectRenderFrame(frame(nestedWidget, 6, targetFor('s1'))));
    assert.equal(container.querySelectorAll('[data-variant="conversation.input.above"]').length, 1);
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    container.remove();
    for (const key of Object.keys(window.localStorage)) if (!keysBefore.has(key)) window.localStorage.removeItem(key);
  }
});

test('the plugin settings switch remains available while off after catalog refresh and settings remount', async () => {
  const transport = createMockTransport();
  const packageId = 'settings-page-visibility-test';
  const binding = { key: 'visible', default: true };
  const keysBefore = new Set(Object.keys(window.localStorage));
  const declaration = {
    package_id: packageId,
    id: 'show',
    kind: 'item',
    location: { menu: 'general', section: 'display' },
    scope: 'application',
    instance_key: 'show',
    presentation: 'row',
    title: { zh: '显示测试用量', en: 'Show test usage' },
  };
  let revision = '1';
  let renderRevision = 0;
  const dispatch = transport.dispatch;
  transport.dispatch = async (request) => {
    const result = await dispatch(request);
    if (request.event === 'ui.ready' && request.target.slotId === 'application.settings') {
      transport.injectRenderFrame(
        frame({ component: 'Switch', props: { preference: binding, ariaLabel: 'Usage display' } }, ++renderRevision, {
          session_id: '',
          scope: 'application',
          slot_id: request.target.slotId,
          package_id: packageId,
          instance_key: 'show',
        }),
      );
    }
    return result;
  };
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const definition = {
    id: 'test',
    compositionMode: 'base',
    accessPolicy: { evaluate: () => ({ level: 'editable' }) },
    modules: [
      {
        id: 'general',
        titleKey: 'settingsPanel.modules.general.title',
        icon: () => null,
        sections: [{ id: 'display', items: [] }],
      },
    ],
  };
  const request = async () => ({ revision, items: [declaration], errors: {} });
  function PluginSettings() {
    const settings = usePluginSettings(definition);
    const Control = settings.definition.modules[0].sections[0].items[0]?.render;
    return Control ? createElement(Control) : null;
  }
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const page = (key) =>
    createElement(
      WebUIProvider,
      { runtime, registry: componentRendererRegistry },
      createElement(
        SettingsServicesProvider,
        { request, isConnected: true, connectionState: 'ready' },
        createElement(PluginSettings, { key }),
      ),
    );
  const control = () => container.querySelector('[data-testid="webui-switch"]');
  try {
    await act(async () => root.render(page('first')));
    assert.ok(control());
    await click(control());
    const off = control();
    assert.equal(off.getAttribute('aria-checked'), 'false');
    await act(async () => window.dispatchEvent(new window.Event('focus')));
    assert.equal(control(), off);
    revision = '2';
    await act(async () => window.dispatchEvent(new window.Event('focus')));
    assert.ok(control());
    assert.equal(control().getAttribute('aria-checked'), 'false');
    await act(async () => root.render(page('second')));
    assert.ok(control());
    assert.equal(control().getAttribute('aria-checked'), 'false');
    await click(control());
    assert.equal(control().getAttribute('aria-checked'), 'true');
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    container.remove();
    for (const key of Object.keys(window.localStorage)) if (!keysBefore.has(key)) window.localStorage.removeItem(key);
  }
});

test('one settings tree mounts application and session items with independent identities', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const keysBefore = new Set(Object.keys(window.localStorage));
  const declarations = [
    {
      package_id: 'unified-settings-test',
      id: 'visible',
      kind: 'item',
      scope: 'application',
      instance_key: 'visible',
      presentation: 'row',
      title: { zh: '显示用量', en: 'Show usage' },
      location: { menu: 'general', section: 'display' },
    },
    {
      package_id: 'workbench-settings-test',
      id: 'settings',
      kind: 'item',
      scope: 'session',
      instance_key: 'workbench',
      presentation: 'content',
      location: { menu: 'general', section: 'display' },
    },
  ];
  let revision = 0;
  const dispatch = transport.dispatch;
  transport.dispatch = async (request) => {
    const result = await dispatch(request);
    if (request.event !== 'ui.ready' || request.target.slotId !== 'application.settings') return result;
    const application = request.target.scope === 'application';
    const tree = {
      component: 'Switch',
      props: application
        ? { preference: { key: 'visible', default: true }, ariaLabel: 'Application setting' }
        : { checked: true, ariaLabel: 'Workbench setting' },
    };
    transport.injectRenderFrame(
      frame(tree, ++revision, {
        session_id: request.target.sessionId,
        slot_id: request.target.slotId,
        package_id: request.target.packageId,
        instance_key: request.target.instanceKey,
        ...(application ? { scope: 'application' } : {}),
      }),
    );
    return result;
  };
  const definition = {
    id: 'test',
    compositionMode: 'base',
    accessPolicy: { evaluate: () => ({ level: 'editable' }) },
    modules: [{ id: 'general', titleKey: 'general', icon: () => null, sections: [{ id: 'display', items: [] }] }],
  };
  const request = async () => ({ revision: 'unified-1', items: declarations, errors: {} });
  function SettingsTree({ active }) {
    const settings = usePluginSettings(definition);
    return active
      ? settings.definition.modules[0].sections[0].items.map((item) => createElement(item.render, { key: item.id }))
      : null;
  }
  const page = (sessionId, active = true) =>
    createElement(
      WebUIProvider,
      { runtime, registry: componentRendererRegistry },
      createElement(
        SettingsServicesProvider,
        { request, isConnected: true, connectionState: 'ready' },
        createElement(WebUISlotProvider, { sessionId }, createElement(SettingsTree, { active })),
      ),
    );
  const application = () => container.querySelector('[aria-label="Application setting"]');
  const workbench = () => container.querySelector('[aria-label="Workbench setting"]');
  try {
    await act(async () => root.render(page(null)));
    const original = application();
    assert.ok(original);
    assert.equal(workbench(), null);
    await act(async () => root.render(page('s1')));
    assert.equal(application(), original);
    assert.ok(workbench());
    await click(application());
    assert.equal(application().getAttribute('aria-checked'), 'false');
    assert.equal(workbench().getAttribute('aria-checked'), 'true');
    await act(async () => root.render(page('s2')));
    assert.equal(application(), original);
    assert.ok(workbench());
    await act(async () => root.render(page('s2', false)));
    assert.equal(container.querySelectorAll('[data-testid="webui-host"]').length, 0);
    await act(async () => root.render(page('s2')));
    assert.equal(application().getAttribute('aria-checked'), 'false');
    await click(application());
    assert.equal(application().getAttribute('aria-checked'), 'true');
    assert.ok(container.querySelector('[data-testid="settings-row-main"]'));
  } finally {
    await act(async () => root.unmount());
    runtime.dispose();
    container.remove();
    for (const key of Object.keys(window.localStorage)) if (!keysBefore.has(key)) window.localStorage.removeItem(key);
  }
});

const appearanceCases = JSON.parse(readFileSync(new URL('./fixtures/gatewayAppearanceCases.json', import.meta.url), 'utf8'));
for (const { name, appearance, valid } of appearanceCases) {
  test(`Gateway appearance contract: ${name}`, () => {
    const result = appearanceModule.validateAppearance(appearance);
    if (valid) assert.deepEqual(result, appearance);
    else assert.equal(result, null);
  });
}

test('accent derivation keeps white button text readable and lets tokens override', () => {
  const { accentVariables, parseColor, contrastRatio, buildAppearanceCss } = appearanceModule;
  const white = [255, 255, 255];
  for (const accent of ['#b45309', '#2f6fae', '#0d8f72', '#ffdd00', '#ffffff', '#000000', '#17b893']) {
    const variables = accentVariables(accent);
    const strong = parseColor(variables['--color-action-primary']);
    assert.ok(contrastRatio(strong, white) >= 4.5, `${accent} strong ${variables['--color-action-primary']}`);
    assert.ok(contrastRatio(parseColor(variables['--color-action-primary-hover']), white) >= 3, `${accent} hover`);
    assert.equal(variables['--color-action-primary-rgb'], strong.join(', '));
    assert.equal(variables['--color-text-link'], variables['--color-action-primary']);
    assert.equal(variables['--color-border-accent-rgb'], parseColor(accent).join(', '));
  }
  // A dark enough accent is used as given.
  assert.equal(accentVariables('#2f6fae')['--color-action-primary'], '#2f6fae');
  const css = buildAppearanceCss({ accent: '#b45309', tokens: { '--color-action-primary-subtle': '#ff000020' } }, {});
  assert.match(css, /--color-action-primary-subtle:#ff000020/);
  assert.doesNotMatch(css, /--color-action-primary-subtle:rgba/);
  assert.doesNotMatch(css, /data-webui-background/, 'opaque appearances add no translucent rules');
});

test('translucent preset uses theme bases, concrete colors and only applies with a background', () => {
  const { buildAppearanceCss, readSurfaceBases, translucentVariables } = appearanceModule;
  const bases = readSurfaceBases((name) => (name === '--color-surface-card' ? ' #fafafa' : name === '--color-sidebar-surface' ? 'rgb(1, 2, 3)' : 'var(--x)'));
  assert.deepEqual(bases['--color-surface-card'], [250, 250, 250]);
  assert.deepEqual(bases['--color-sidebar-surface'], [1, 2, 3]);
  assert.deepEqual(bases['--color-surface-page'], [245, 247, 249], 'unreadable bases fall back to the default theme');
  const variables = translucentVariables(0.8, bases);
  assert.equal(variables['--color-surface-page'], 'rgba(245, 247, 249, 0.336)');
  assert.equal(variables['--color-sidebar-surface'], 'rgba(1, 2, 3, 0.56)');
  assert.equal(variables['--color-surface-panel'], 'rgba(255, 255, 255, 0.82)');
  assert.equal(variables['--color-surface-card'], 'rgba(250, 250, 250, 0.88)');
  assert.equal(variables['--color-surface-shell'], 'transparent');
  assert.equal(variables['--color-surface-popover'], undefined);
  assert.ok(Object.keys(variables).every((name) => !name.startsWith('--color-text')));
  const css = buildAppearanceCss({ surface: 'translucent', surfaceOpacity: 0.8 }, bases);
  assert.doesNotMatch(css, /calc\(/);
  for (const rule of css.split('\n')) {
    assert.match(rule, /\[data-webui-surface='translucent'\]\[data-webui-background='active'\]/, rule);
  }
  // Only role variables of the theme are overridden; no component class appears in the sheet.
  assert.match(css, /--color-surface-popover:rgba\(250, 250, 250, 0\.97\)/);
  assert.match(css, /--color-surface-workspace:rgba\(250, 250, 250, 0\.56\)/);
  assert.match(css, /--color-surface-workspace-layer:transparent/);
  assert.match(css, /--color-surface-floating:rgba\(1, 2, 3, 0\.9\)/);
  assert.match(css, /--effect-surface-floating-backdrop:blur\(24px\) saturate\(1\.3\)/);
  assert.doesNotMatch(css.replace(/:root[^{]*\{/g, '{'), /\.[a-z][a-z0-9_-]*[\s,{]/, 'no business selectors');
  assert.match(buildAppearanceCss({ surface: 'translucent' }, bases), /rgba\(245, 247, 249, 0\.361\)/, 'default opacity 0.86');
});

test('WebUIAppearanceHost writes the contribution layer and removes it on revoke and unmount', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  const html = document.documentElement;
  const style = () => document.getElementById('webui-appearance');
  html.setAttribute('data-theme', 'default'); html.setAttribute('data-color-mode', 'light');
  try {
    await act(async () => root.render(createElement(WebUIAppearanceHost, { runtime })));
    assert.equal(style(), null);
    await act(async () => transport.injectAppearanceFrame({ package_id: 'pkg.theme', revision: 1,
      appearance: { accent: '#b45309', surface: 'translucent', surfaceOpacity: 0.6 } }));
    assert.match(style().textContent, /--color-action-primary:#b45309/);
    assert.match(style().textContent, /rgba\(245, 247, 249, 0\.252\)/);
    assert.equal(html.getAttribute('data-webui-surface'), 'translucent');
    const firstWrite = html.getAttribute('data-webui-appearance');
    assert.ok(firstWrite);

    // A theme change recomputes the layer and announces a new write to theme observers.
    await act(async () => { html.setAttribute('data-color-mode', 'dark'); await new Promise((resolve) => setTimeout(resolve, 0)); });
    assert.notEqual(html.getAttribute('data-webui-appearance'), firstWrite);
    assert.equal(html.getAttribute('data-color-mode'), 'dark', 'the host never changes the color mode');

    await act(async () => transport.injectAppearanceFrame({ package_id: 'pkg.theme', revision: 2, appearance: null }));
    assert.equal(style(), null);
    assert.equal(html.hasAttribute('data-webui-appearance'), false);
    assert.equal(html.hasAttribute('data-webui-surface'), false);
    assert.equal(html.getAttribute('data-color-mode'), 'dark');

    await act(async () => transport.injectAppearanceFrame({ package_id: 'pkg.theme', revision: 3, appearance: { accent: '#2f6fae' } }));
    assert.equal(html.getAttribute('data-webui-surface'), 'opaque');
    await act(async () => root.unmount());
    assert.equal(style(), null, 'unmounting the host removes the layer');
  } finally {
    runtime.dispose();
    container.remove();
    html.setAttribute('data-color-mode', 'light');
  }
});

// ── Startup splash (Gateway F_12) ────────────────────────────────────────────

const splashSurface = (src) => ({ component: 'MediaSurface', props: { kind: 'html', src, title: 'Boot' } });

function splashTransport(answers, { connected = true } = {}) {
  const handlers = new Set();
  const dispatches = [];
  let state = connected ? 'connected' : 'disconnected';
  let revision = 0;
  const target = (packageId, slot = 'application.splash') => ({
    scope: 'application', slot_id: slot, package_id: packageId, instance_key: 'main',
  });
  const transport = {
    dispatches,
    subscribe(handler) { handlers.add(handler); return () => handlers.delete(handler); },
    getConnectionState: () => state,
    setConnectionState(next) { state = next; for (const handler of handlers) handler.onConnectionStateChange(next); },
    render(packageId, root, slot) {
      revision += 1;
      for (const handler of handlers) {
        handler.onRenderFrame({ target: target(packageId, slot), schema_version: 1, revision, mode: 'replace', root });
      }
    },
    clear(packageId) {
      revision += 1;
      for (const handler of handlers) handler.onClearFrame({ target: target(packageId), revision });
    },
    async dispatch(request) {
      dispatches.push(request);
      if (request.event !== 'ui.ready' || request.target.slotId !== 'application.splash') {
        return { accepted: true, status: request.event === 'ui.closed' ? 'closed' : 'ready' };
      }
      const answer = answers[request.target.packageId] ?? 'unhandled';
      if (answer === 'hang') return new Promise(() => undefined);
      if (answer === 'play') {
        transport.render(request.target.packageId, splashSurface(`/extension-assets/${request.target.packageId}/g/boot.html`));
        return { accepted: true, status: 'playing' };
      }
      return answer === 'skip' ? { accepted: true, status: 'skipped' } : { accepted: false, status: 'unhandled' };
    },
  };
  return transport;
}

function fakeTimers() {
  let now = 0;
  let sequence = 0;
  const pending = new Map();
  return {
    set(callback, ms) { sequence += 1; pending.set(sequence, { at: now + ms, callback }); return sequence; },
    clear(handle) { pending.delete(handle); },
    async advance(ms) {
      now += ms;
      for (const [handle, timer] of [...pending].sort((left, right) => left[1].at - right[1].at)) {
        if (timer.at <= now && pending.has(handle)) { pending.delete(handle); timer.callback(); await settle(); }
      }
      await settle();
    },
  };
}

async function settle() {
  for (let index = 0; index < 5; index += 1) await new Promise((resolve) => setTimeout(resolve, 0));
}

function splashLaunch(completed = false) {
  return { id: 'launch-1', completed, firstVisit: true, marks: 0, markCompleted() { this.completed = true; this.marks += 1; } };
}

const providerList = (...entries) => ({ providers: entries.map(([packageId, skippable = false, max = 60000]) => ({
  package_id: packageId, skippable, max_duration_ms: max,
})) });

function splashSetup({ answers = {}, providers = providerList(), hint = 'candidates', launch = splashLaunch(), preferred, connected } = {}) {
  const transport = splashTransport(answers, { connected });
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const timers = fakeTimers();
  const requests = [];
  const controller = splashModule.createSplashController({
    runtime, launch, timers, preferredProvider: preferred, reducedMotion: () => false,
    hint: async () => hint,
    requestProviders: async () => { requests.push('providers'); return providers; },
  });
  const phases = [];
  controller.subscribe(() => phases.push(`${controller.getState().phase}:${controller.getState().provider?.packageId ?? ''}`));
  const readies = () => transport.dispatches.filter((request) => request.event === 'ui.ready').map((request) => request.target.packageId);
  const closes = () => transport.dispatches.filter((request) => request.event === 'ui.closed')
    .map((request) => [request.target.packageId, request.payload.reason, request.payload.revision]);
  return { transport, runtime, timers, controller, requests, phases, readies, closes, launch };
}

test('splash provider lists are validated and the appearance provider plays first', () => {
  const { parseProviders, orderProviders } = splashModule;
  const providers = parseProviders({ providers: [
    { package_id: 'pkg.a', skippable: true, max_duration_ms: 9500 },
    { package_id: 'pkg.b', skippable: false, max_duration_ms: 90000 },
    { package_id: 'pkg.a', skippable: false, max_duration_ms: 1000 },
    { package_id: '', skippable: true, max_duration_ms: 1000 },
    { package_id: 'pkg.c', skippable: 'yes', max_duration_ms: 1000 },
    { package_id: 'pkg.d', skippable: true, max_duration_ms: 1.5 },
    'pkg.e',
  ] });
  assert.deepEqual(providers, [
    { packageId: 'pkg.a', skippable: true, maxDurationMs: 9500 },
    { packageId: 'pkg.b', skippable: false, maxDurationMs: 60000 },
  ]);
  assert.deepEqual(orderProviders(providers, 'pkg.b').map((provider) => provider.packageId), ['pkg.b', 'pkg.a']);
  assert.deepEqual(orderProviders(providers, 'pkg.z').map((provider) => provider.packageId), ['pkg.a', 'pkg.b']);
  assert.deepEqual(parseProviders(null), []);
});

test('splash gate opens at once for a completed launch or a hint without candidates', async () => {
  for (const options of [{ launch: splashLaunch(true) }, { hint: 'none' }]) {
    const app = splashSetup(options);
    await app.controller.start();
    assert.equal(app.controller.getState().phase, 'open');
    assert.equal(app.phases[0], 'open:', 'the gate opens before the Gateway answers');
    assert.deepEqual(app.requests, options.hint ? ['providers'] : [], 'a "none" hint is still confirmed with the Gateway');
    assert.deepEqual(app.readies(), []);
    app.controller.dispose(); app.runtime.dispose();
  }
  assert.equal(window.localStorage.getItem('workswarm.splash.expected'), '0');
});

test('a "none" hint cannot hide a splash the Gateway lists', async () => {
  const app = splashSetup({ hint: 'none', answers: { 'pkg.a': 'skip' }, providers: providerList(['pkg.a']) });
  await app.controller.start();
  assert.equal(app.phases[0], 'open:', 'the workspace is not held back by the hint');
  assert.deepEqual(app.requests, ['providers']);
  assert.deepEqual(app.readies(), ['pkg.a'], 'the provider the Gateway listed is asked to play');
  assert.equal(app.controller.getState().phase, 'open');
  assert.equal(window.localStorage.getItem('workswarm.splash.expected'), '1');
  app.controller.dispose(); app.runtime.dispose();
  window.localStorage.removeItem('workswarm.splash.expected');
});

test('splash queue plays providers in order, skips non-players and closes each played splash once', async () => {
  const app = splashSetup({
    answers: { 'pkg.a': 'play', 'pkg.b': 'play', 'pkg.skip': 'skip', 'pkg.none': 'unhandled' },
    providers: providerList(['pkg.a', true], ['pkg.skip'], ['pkg.b', false], ['pkg.none']),
    preferred: 'pkg.b',
  });
  const done = app.controller.start();
  await settle();
  assert.deepEqual(app.readies(), ['pkg.b']);
  assert.equal(app.controller.getState().phase, 'playing');
  assert.equal(app.controller.getState().provider.packageId, 'pkg.b');
  const readyPayload = app.transport.dispatches[0].payload;
  assert.deepEqual(readyPayload, { launch_id: 'launch-1', first_visit: true, reduced_motion: false, preview: false });

  app.controller.skip();
  await settle();
  assert.equal(app.controller.getState().provider.packageId, 'pkg.b', 'a non-skippable splash ignores skip');
  app.controller.surfaceEvent('ready');
  app.controller.surfaceEvent('complete');
  await settle();
  assert.deepEqual(app.readies(), ['pkg.b', 'pkg.a']);
  app.controller.surfaceEvent('ready');
  app.controller.skip();
  await done;
  assert.deepEqual(app.readies(), ['pkg.b', 'pkg.a', 'pkg.skip', 'pkg.none']);
  const closes = app.closes();
  assert.deepEqual(closes.map(([packageId, reason]) => [packageId, reason]), [['pkg.b', 'complete'], ['pkg.a', 'skip']]);
  assert.ok(closes.every(([, , revision]) => Number.isInteger(revision) && revision > 0));
  assert.equal(app.controller.getState().phase, 'open');
  assert.equal(app.launch.marks, 1);
  assert.equal(window.localStorage.getItem('workswarm.splash.played'), '1');
  assert.equal(window.localStorage.getItem('workswarm.splash.expected'), '1');
  app.runtime.dispose();
});

test('splash time limits end preparation and playback without blocking the gate', async () => {
  const app = splashSetup({
    answers: { 'pkg.hang': 'hang', 'pkg.slow': 'play', 'pkg.silent': 'play', 'pkg.long': 'play', 'pkg.cleared': 'play' },
    providers: providerList(['pkg.hang'], ['pkg.slow'], ['pkg.silent'], ['pkg.long', false, 4000], ['pkg.cleared']),
  });
  const done = app.controller.start();
  await settle();
  assert.equal(app.controller.getState().phase, 'preparing');
  await app.timers.advance(splashModule.SPLASH_TIMEOUTS.answer);      // no answer from pkg.hang
  assert.equal(app.controller.getState().provider.packageId, 'pkg.slow');
  await app.timers.advance(splashModule.SPLASH_TIMEOUTS.mediaReady);  // pkg.slow never became ready
  assert.equal(app.controller.getState().provider.packageId, 'pkg.silent');
  app.controller.surfaceEvent('ready');
  await app.timers.advance(4000);
  app.controller.surfaceEvent('heartbeat');
  await app.timers.advance(4000);
  assert.equal(app.controller.getState().provider.packageId, 'pkg.silent', 'heartbeats keep a splash alive');
  await app.timers.advance(splashModule.SPLASH_TIMEOUTS.heartbeat);  // heartbeats stopped
  assert.equal(app.controller.getState().provider.packageId, 'pkg.long');
  app.controller.surfaceEvent('ready');
  for (let elapsed = 0; elapsed < 4000; elapsed += 1000) {
    app.controller.surfaceEvent('heartbeat');
    await app.timers.advance(1000);                                   // reaches the declared maximum
  }
  assert.equal(app.controller.getState().provider.packageId, 'pkg.cleared');
  app.transport.clear('pkg.cleared');                                 // the provider ends its own splash
  await done;
  assert.deepEqual(app.closes().map(([packageId, reason]) => [packageId, reason]),
    [['pkg.slow', 'timeout'], ['pkg.silent', 'timeout'], ['pkg.long', 'timeout']]);
  assert.equal(app.controller.getState().phase, 'open');
  app.runtime.dispose();
});

test('splash gate stops waiting for an unreachable Gateway', async () => {
  for (const [hint, wait] of [['unknown', splashModule.SPLASH_TIMEOUTS.connectUnknown], ['candidates', splashModule.SPLASH_TIMEOUTS.connectHinted]]) {
    const app = splashSetup({ hint, connected: false, providers: providerList(['pkg.a']), answers: { 'pkg.a': 'play' } });
    const done = app.controller.start();
    await settle();
    assert.equal(app.controller.getState().phase, 'deciding');
    await app.timers.advance(wait - 1);
    assert.equal(app.controller.getState().phase, 'deciding');
    await app.timers.advance(1);
    await done;
    assert.equal(app.controller.getState().phase, 'open');
    assert.deepEqual(app.requests, []);
    app.runtime.dispose();
  }
  const app = splashSetup({ hint: 'candidates', connected: false, providers: providerList(['pkg.a']), answers: { 'pkg.a': 'skip' } });
  const done = app.controller.start();
  await settle();
  app.transport.setConnectionState('connected');
  await done;
  assert.deepEqual(app.readies(), ['pkg.a'], 'the queue starts once the Gateway connects');
  app.runtime.dispose();
});

test('WebUISplashGate holds the workspace and entry until the queue ends and skips on a held Space', async () => {
  const transport = splashTransport({ 'pkg.theme': 'play' });
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const rootElement = document.createElement('div'); rootElement.id = 'root'; document.body.append(rootElement);
  const neutral = document.createElement('div'); neutral.id = 'workswarm-neutral'; document.body.append(neutral);
  const reactRoot = createRoot(rootElement);
  const launch = splashLaunch();
  const overlay = () => document.querySelector('[data-testid="webui-splash"]');
  try {
    await act(async () => reactRoot.render(createElement(WebUIProvider, { runtime, registry: componentRendererRegistry },
      createElement(WebUISplashGate, { launch, hint: async () => 'candidates',
        requestProviders: async () => providerList(['pkg.theme', true, 9500]) },
        createElement(WebUIEntryHost, null, createElement('main', { 'data-testid': 'workspace' }, 'Workspace'))))));
    assert.equal(document.getElementById('workswarm-neutral'), null, 'the gate takes over the first-paint layer');
    assert.equal(overlay().dataset.look, 'neutral');
    assert.equal(rootElement.hasAttribute('inert'), true);
    assert.ok(!rootElement.contains(overlay()), 'the splash layer lives outside the inert root');

    // A login entry rendered during the splash waits for the gate.
    await act(async () => transport.render('pkg.login', { component: 'Text', props: { value: 'Sign in' } }, 'application.entry'));
    await act(async () => settle());
    assert.equal(overlay().dataset.variant, 'playing');
    assert.equal(document.querySelector('[data-testid="webui-entry-host"]').open, false);
    const frame = overlay().querySelector('[data-testid="webui-media-surface-frame"]');
    assert.ok(frame, 'the splash surface plays inside the overlay');
    assert.equal(frame.getAttribute('allow'), 'autoplay', 'a splash may play its sound');
    const token = surfaceToken(frame);
    await postSurface(frame.contentWindow, surfaceMessage(token, 'ready'));
    assert.ok(overlay().querySelector('[data-testid="webui-splash-skip"]'));
    assert.match(overlay().querySelector('[data-testid="webui-splash-skip-label"]').textContent, /空格|Space/);

    await act(async () => window.dispatchEvent(new window.KeyboardEvent('keydown', { code: 'Space', bubbles: true })));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 300)));
    await act(async () => window.dispatchEvent(new window.KeyboardEvent('keyup', { code: 'Space', bubbles: true })));
    assert.ok(overlay(), 'releasing early does not skip');
    await act(async () => window.dispatchEvent(new window.KeyboardEvent('keydown', { code: 'Space', bubbles: true })));
    await act(async () => new Promise((resolve) => setTimeout(resolve, 1000)));
    await act(async () => settle());

    assert.equal(overlay(), null);
    assert.equal(rootElement.hasAttribute('inert'), false);
    assert.equal(document.querySelector('[data-testid="webui-entry-host"]').open, true, 'the entry shows after the gate');
    const closed = transport.dispatches.filter((request) => request.event === 'ui.closed');
    assert.deepEqual(closed.map((request) => [request.target.packageId, request.payload.reason]), [['pkg.theme', 'skip']]);
    assert.equal(launch.marks, 1);
  } finally {
    await act(async () => reactRoot.unmount());
    runtime.dispose();
    rootElement.remove();
    document.getElementById('workswarm-neutral')?.remove();
  }
});

function sessionEntries() {
  return Object.fromEntries(Object.keys(window.sessionStorage).map((key) => [key, window.sessionStorage.getItem(key)]));
}

function openTab(entries = {}) {
  window.sessionStorage.clear();
  for (const [key, value] of Object.entries(entries)) window.sessionStorage.setItem(key, value);
}

/** Runs the first-paint script of index.html; true when it keeps the neutral layer. */
function neutralLayerKept() {
  const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
  const script = [...html.matchAll(/<script>([\s\S]*?)<\/script>/g)].map((match) => match[1])
    .find((body) => body.includes('workswarm-neutral'));
  const neutral = document.createElement('div'); neutral.id = 'workswarm-neutral'; document.body.append(neutral);
  try {
    // The page script uses the bare storage globals of a browser window.
    new Function('window', 'document', 'localStorage', 'sessionStorage', script)(
      window, document, window.localStorage, window.sessionStorage);
    return document.getElementById('workswarm-neutral') !== null;
  } finally {
    document.getElementById('workswarm-neutral')?.remove();
  }
}

test('each browser tab keeps its own splash completion through reloads', () => {
  const keysBefore = new Set(Object.keys(window.localStorage));
  const sessionBefore = sessionEntries();
  try {
    window.localStorage.setItem(splashModule.SPLASH_EXPECTED_KEY, '1');
    openTab();
    const first = splashModule.resolveLaunch();
    assert.equal(first.completed, false);
    assert.equal(neutralLayerKept(), true);
    first.markCompleted();
    const firstTab = sessionEntries();

    openTab();  // a second tab starts its own launch and finishes its splash
    const second = splashModule.resolveLaunch();
    assert.notEqual(second.id, first.id);
    assert.equal(second.completed, false);
    second.markCompleted();

    openTab(firstTab);  // the first tab reloads
    const reloaded = splashModule.resolveLaunch();
    assert.equal(reloaded.id, first.id);
    assert.equal(reloaded.completed, true, 'the other tab did not overwrite this tab\'s record');
    assert.equal(neutralLayerKept(), false, 'the first-paint script reads the same record');

    openTab();  // a fresh tab still plays
    assert.equal(splashModule.resolveLaunch().completed, false);
  } finally {
    openTab(sessionBefore);
    for (const key of Object.keys(window.localStorage)) if (!keysBefore.has(key)) window.localStorage.removeItem(key);
  }
});

test('desktop windows of one start share the completion and a new start plays again', () => {
  const keysBefore = new Set(Object.keys(window.localStorage));
  const sessionBefore = sessionEntries();
  try {
    window.localStorage.setItem(splashModule.SPLASH_EXPECTED_KEY, '1');
    window.jiuwenDesktop = { launchId: 'desktop-start-1' };
    openTab();
    const main = splashModule.resolveLaunch('desktop-start-1');
    assert.equal(main.completed, false);
    main.markCompleted();
    openTab();  // another window of the same start
    assert.equal(splashModule.resolveLaunch('desktop-start-1').completed, true);
    assert.equal(neutralLayerKept(), false);
    window.jiuwenDesktop = { launchId: 'desktop-start-2' };
    assert.equal(splashModule.resolveLaunch('desktop-start-2').completed, false);
    assert.equal(neutralLayerKept(), true);
  } finally {
    delete window.jiuwenDesktop;
    openTab(sessionBefore);
    for (const key of Object.keys(window.localStorage)) if (!keysBefore.has(key)) window.localStorage.removeItem(key);
  }
});

// ── Application settings and file transfers (Gateway F_13) ───────────────────

const {
  transfersModule, applicationSettingsModule, WebUIUploadContext, SplashProviderSettings,
  extensionsModule,
} = await import('../node_modules/.cache/webui-components/components.mjs');

// Gateway packages render their settings items in the shared settings slot (AgentServer S_18).
const SETTINGS_SLOT = 'application.settings';
const importTree = (props = { label: 'Import theme', accept: ['.zip'] }) => ({ component: 'Box', children: [
  { component: 'FileImport', props, events: { 'ui.change': { id: 'import' } } },
] });

function settingsRequest(handler) {
  const calls = [];
  return { calls, request: async (method, params) => { calls.push([method, params]); return handler(method, params); } };
}

async function mounted(element) {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  await act(async () => root.render(element));
  await act(async () => settle());
  return { container, async unmount() { await act(async () => root.unmount()); container.remove(); } };
}

test('application settings answers are validated and the shown order follows the saved choice', () => {
  const { parseSplashSettings, displayedProviders, movedOrder, toggledDisabled } = applicationSettingsModule;
  // Manifest display.name arrives as { zh, en }; both languages are required and extra fields are dropped.
  assert.deepEqual(parseSplashSettings({ customized: false, providers: [
    { package_id: 'pkg.l', title: { zh: '主题背景', en: 'Theme background', extra: 1 }, skippable: true,
      max_duration_ms: 9500, enabled: true },
    { package_id: 'pkg.m', title: { zh: '只有中文' }, skippable: true, max_duration_ms: 9500, enabled: true },
  ] }).providers.map((provider) => provider.title), [{ zh: '主题背景', en: 'Theme background' }]);
  const settings = parseSplashSettings({ customized: false, providers: [
    { package_id: 'pkg.a', title: 'A', skippable: true, max_duration_ms: 9500, enabled: true },
    { package_id: 'pkg.b', title: 'B', skippable: false, max_duration_ms: 60000, enabled: false },
    { package_id: 'pkg.c', title: 'C', skippable: false, max_duration_ms: 60000, enabled: true },
    { package_id: 'pkg.d', title: 'D', skippable: false, max_duration_ms: 1.5, enabled: true },
  ] });
  assert.deepEqual(settings.providers.map((provider) => provider.packageId), ['pkg.a', 'pkg.b', 'pkg.c']);
  assert.equal(parseSplashSettings({ providers: [] }), null);
  assert.deepEqual(displayedProviders(settings, 'pkg.c').map((provider) => provider.packageId), ['pkg.c', 'pkg.a', 'pkg.b']);
  assert.deepEqual(displayedProviders({ ...settings, customized: true }, 'pkg.c').map((provider) => provider.packageId),
    ['pkg.a', 'pkg.b', 'pkg.c']);
  assert.deepEqual(movedOrder(settings.providers, 'pkg.b', -1), ['pkg.b', 'pkg.a', 'pkg.c']);
  assert.equal(movedOrder(settings.providers, 'pkg.a', -1), null);
  assert.equal(movedOrder(settings.providers, 'pkg.c', 1), null);
  assert.deepEqual(toggledDisabled(settings.providers, 'pkg.a', false), ['pkg.a', 'pkg.b']);
  assert.deepEqual(toggledDisabled(settings.providers, 'pkg.b', true), []);
});

test('a saved splash order plays as saved instead of putting the appearance provider first', async () => {
  const app = splashSetup({
    answers: { 'pkg.a': 'skip', 'pkg.b': 'skip' },
    providers: { ...providerList(['pkg.a'], ['pkg.b']), customized: true },
    preferred: 'pkg.b',
  });
  await app.controller.start();
  assert.deepEqual(app.readies(), ['pkg.a', 'pkg.b']);
  assert.equal(splashModule.parseCustomized({ customized: 'yes' }), false);
  assert.equal(splashModule.parseCustomized({ customized: true }), true);
  app.runtime.dispose();
});

test('upload service starts from the current frame, sends each file and returns the ticket', async () => {
  const transport = splashTransport({});
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const handle = runtime.mountSlot('', SETTINGS_SLOT, { packageId: 'pkg.theme', instanceKey: 'main', notifyReady: false });
  transport.render('pkg.theme', importTree(), SETTINGS_SLOT);
  const key = runtime.getScopeSnapshot('', SETTINGS_SLOT).instanceKeys[0];
  const id = 'a'.repeat(32);
  const requests = [];
  const puts = [];
  const service = transfersModule.createUploadService({
    runtime,
    request: async (method, params) => { requests.push([method, params]); return { upload_id: id, url: `/extension-uploads/${id}/` }; },
    put: async (url, body) => { puts.push([url, body.size]); return 204; },
  });
  const progress = [];
  const files = transfersModule.describePickedFiles([
    { name: 'theme one.json', webkitRelativePath: 'hive/theme one.json', size: 2 },
    { name: '媒体.png', webkitRelativePath: 'hive/媒体.png', size: 3 },
  ]).map((file) => ({ ...file, body: new Blob(['x'.repeat(file.size)]) }));
  try {
    assert.equal(await service.upload(key, '0.0', files, (step) => progress.push(step.sentBytes)), id);
    assert.deepEqual(requests, [['webui.upload.begin', {
      target: { scope: 'application', slot_id: SETTINGS_SLOT, package_id: 'pkg.theme', instance_key: 'main' },
      node_key: '0.0', revision: runtime.getInstance(key).frame.revision,
      files: [{ path: 'hive/theme one.json', size: 2 }, { path: 'hive/媒体.png', size: 3 }],
    }]]);
    assert.deepEqual(puts, [
      [`/extension-uploads/${id}/hive/theme%20one.json`, 2], [`/extension-uploads/${id}/hive/%E5%AA%92%E4%BD%93.png`, 3],
    ]);
    assert.deepEqual(progress, [0, 2, 5]);
    const failing = (request, put) => transfersModule.createUploadService({ runtime, request, put });
    const ticket = async () => ({ upload_id: id, url: `/extension-uploads/${id}/` });
    await assert.rejects(failing(async () => { throw Object.assign(new Error('large'), { code: 'UPLOAD_TOO_LARGE' }); },
      async () => 204).upload(key, '0.0', files), { code: 'UPLOAD_TOO_LARGE' });
    await assert.rejects(failing(async () => ({ upload_id: id, url: '/elsewhere/' }), async () => 204)
      .upload(key, '0.0', files), { code: 'UPLOAD_FAILED' });
    await assert.rejects(failing(ticket, async () => 403).upload(key, '0.0', files), { code: 'UPLOAD_FAILED' });
    await assert.rejects(failing(ticket, async () => { throw new Error('offline'); }).upload(key, '0.0', files),
      { code: 'UPLOAD_FAILED' });
    await assert.rejects(service.upload('missing', '0.0', files), { code: 'TARGET_INACTIVE' });
  } finally {
    handle.release();
    runtime.dispose();
  }
});

test('FileImport uploads picked files through the host service and emits the ticket', async () => {
  const transport = splashTransport({});
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const id = 'b'.repeat(32);
  const calls = [];
  let outcome = 'ok';
  const service = {
    async upload(instanceKey, nodeKey, files, onProgress) {
      calls.push([nodeKey, files.map((file) => [file.path, file.size])]);
      onProgress?.({ sentBytes: 1, totalBytes: 2 });
      if (outcome !== 'ok') throw new transfersModule.WebUIUploadError(outcome);
      return id;
    },
  };
  const view = await mounted(createElement(WebUIProvider, { runtime, registry: componentRendererRegistry },
    createElement(WebUIUploadContext.Provider, { value: service },
      createElement(WebUIHost, { slotId: SETTINGS_SLOT, scope: 'application', packageId: 'pkg.theme', instanceKey: 'main' }))));
  const find = (testId) => view.container.querySelector(`[data-testid="${testId}"]`);
  const pick = async (files) => {
    const input = find('webui-file-import-input');
    Object.defineProperty(input, 'files', { configurable: true, value: files });
    await act(async () => input.dispatchEvent(new window.Event('change', { bubbles: true })));
    await act(async () => settle());
  };
  try {
    await act(async () => transport.render('pkg.theme', importTree(), SETTINGS_SLOT));
    await act(async () => settle());
    assert.equal(find('webui-file-import-input').getAttribute('accept'), '.zip');
    assert.equal(find('webui-file-import-input').multiple, false);
    assert.equal(find('webui-file-import-btn').textContent, 'Import theme');
    assert.equal(find('webui-file-import-btn').disabled, false);
    await pick([new window.File(['zip!'], 'aurora.zip')]);
    assert.deepEqual(calls, [['0.0', [['aurora.zip', 4]]]]);
    assert.deepEqual(transport.dispatches.filter((request) => request.event === 'ui.change')
      .map((request) => request.payload.data), [{ upload_id: id }]);
    assert.equal(find('webui-file-import').dataset.variant, 'done');
    outcome = 'UPLOAD_TOO_LARGE';
    await pick([new window.File(['zip!'], 'big.zip')]);
    assert.equal(find('webui-file-import').dataset.variant, 'error');
    assert.match(find('webui-file-import-status').textContent, /太大|too large/);
    assert.equal(transport.dispatches.filter((request) => request.event === 'ui.change').length, 1);
    await act(async () => transport.render('pkg.theme', importTree({ label: 'Import folder', directory: true }), SETTINGS_SLOT));
    await act(async () => settle());
    assert.equal(find('webui-file-import-input').hasAttribute('webkitdirectory'), true);
    assert.equal(find('webui-file-import-input').multiple, true);
  } finally {
    await view.unmount();
    runtime.dispose();
  }
});

test('FileImport stays disabled without a host import service', async () => {
  const transport = splashTransport({});
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const view = await mounted(createElement(WebUIProvider, { runtime, registry: componentRendererRegistry },
    createElement(WebUIHost, { slotId: SETTINGS_SLOT, scope: 'application', packageId: 'pkg.theme', instanceKey: 'main' })));
  try {
    await act(async () => transport.render('pkg.theme', importTree(), SETTINGS_SLOT));
    await act(async () => settle());
    assert.equal(view.container.querySelector('[data-testid="webui-file-import-btn"]').disabled, true);
  } finally {
    await view.unmount();
    runtime.dispose();
  }
});

test('download offers are validated and go to the desktop dialog or a download link', async () => {
  const { readDownloadOffer, startDownload } = transfersModule;
  const good = { package_id: 'pkg.theme', url: `/extension-downloads/${'c'.repeat(32)}`, filename: '备份.zip', size: 12 };
  assert.deepEqual(readDownloadOffer(good), { packageId: 'pkg.theme', url: good.url, filename: '备份.zip', size: 12 });
  for (const bad of [
    { ...good, url: 'https://example.invalid/x' }, { ...good, url: '/extension-assets/a/b/c' }, { ...good, filename: '../x' },
    { ...good, filename: ' ' }, { ...good, size: -1 }, { ...good, package_id: '' }, null,
  ]) assert.equal(readDownloadOffer(bad), null, JSON.stringify(bad));
  const offer = readDownloadOffer(good);
  const saved = [];
  await startDownload(offer, { desktopDownload: async (url, name) => saved.push([url, name]), document });
  assert.deepEqual(saved, [[good.url, '备份.zip']]);
  const clicks = [];
  const listener = (event) => {
    if (!(event.target instanceof window.HTMLAnchorElement)) return;
    clicks.push([event.target.getAttribute('href'), event.target.download]);
    event.preventDefault();
  };
  document.addEventListener('click', listener);
  try {
    await startDownload(offer, { document });
  } finally {
    document.removeEventListener('click', listener);
  }
  assert.deepEqual(clicks, [[good.url, '备份.zip']]);
  assert.equal(document.querySelector('a[download]'), null);
});

test('Extensions settings lists splash providers and saves order, switches and reset', async () => {
  window.localStorage.setItem('workswarm.appearance.provider', 'pkg.b');
  let state = { customized: false, providers: [
    { package_id: 'pkg.a', title: 'Aurora', skippable: true, max_duration_ms: 9500, enabled: true },
    { package_id: 'pkg.b', title: 'Hive', skippable: false, max_duration_ms: 60000, enabled: true },
  ] };
  const services = settingsRequest((method, params) => {
    if (method === 'webui.splash.settings.update' && 'order' in params) {
      state = params.order === null ? { ...state, customized: false }
        : { customized: true, providers: params.order.map((id) => state.providers.find((item) => item.package_id === id)) };
    }
    if (method === 'webui.splash.settings.update' && 'disabled' in params) {
      state = { ...state, providers: state.providers.map((item) => ({ ...item, enabled: !params.disabled.includes(item.package_id) })) };
    }
    return state;
  });
  const view = await mounted(createElement(SettingsServicesProvider, { isConnected: true, connectionState: 'ready',
    request: services.request }, createElement(SplashProviderSettings, { disabled: false })));
  const all = (testId) => [...view.container.querySelectorAll(`[data-testid="${testId}"]`)];
  const items = () => all('settings-extensions-splash-item').map((node) => node.dataset.variant);
  const control = (testId, packageId) => all(testId).find((node) => node.dataset.variant === packageId);
  const click = async (node) => { await act(async () => node.click()); await act(async () => settle()); };
  try {
    assert.deepEqual(items(), ['pkg.b', 'pkg.a'], 'without a saved order the appearance provider is shown first');
    assert.equal(view.container.querySelector('[data-testid="settings-extensions-splash-order"]').dataset.variant, 'default');
    assert.equal(view.container.querySelector('[data-testid="settings-extensions-splash-reset-btn"]'), null);
    assert.equal(control('settings-extensions-splash-up-btn', 'pkg.b').disabled, true);
    assert.match(all('settings-extensions-splash-item')[1].closest('.setting-row').textContent, /可跳过，最长 10 秒|Skippable, up to 10 s/);

    await click(control('settings-extensions-splash-down-btn', 'pkg.b'));
    assert.deepEqual(services.calls.at(-1), ['webui.splash.settings.update', { order: ['pkg.a', 'pkg.b'] }]);
    assert.deepEqual(items(), ['pkg.a', 'pkg.b']);
    assert.equal(view.container.querySelector('[data-testid="settings-extensions-splash-order"]').dataset.variant, 'custom');

    await click(control('settings-extensions-splash-switch', 'pkg.a'));
    assert.deepEqual(services.calls.at(-1), ['webui.splash.settings.update', { disabled: ['pkg.a'] }]);
    assert.equal(control('settings-extensions-splash-switch', 'pkg.a').getAttribute('aria-checked'), 'false');

    await click(view.container.querySelector('[data-testid="settings-extensions-splash-reset-btn"]'));
    assert.deepEqual(services.calls.at(-1), ['webui.splash.settings.update', { order: null }]);
    assert.deepEqual(items(), ['pkg.b', 'pkg.a']);
  } finally {
    await view.unmount();
    window.localStorage.removeItem('workswarm.appearance.provider');
  }
});

test('Extensions settings reports a failed save and keeps the previous state', async () => {
  const state = { customized: false, providers: [
    { package_id: 'pkg.a', title: 'Aurora', skippable: true, max_duration_ms: 9500, enabled: true },
  ] };
  const services = settingsRequest((method) => {
    if (method === 'webui.splash.settings.update') throw Object.assign(new Error('write failed'), { code: 'SETTINGS_WRITE_FAILED' });
    return state;
  });
  const view = await mounted(createElement(SettingsServicesProvider, { isConnected: true, connectionState: 'ready',
    request: services.request }, createElement(SplashProviderSettings, { disabled: false })));
  const toggle = () => view.container.querySelector('[data-testid="settings-extensions-splash-switch"]');
  try {
    assert.equal(view.container.querySelector('[data-testid="settings-extensions-splash-save-error"]'), null);
    await act(async () => toggle().click());
    await act(async () => settle());
    assert.deepEqual(services.calls.at(-1), ['webui.splash.settings.update', { disabled: ['pkg.a'] }]);
    assert.ok(view.container.querySelector('[data-testid="settings-extensions-splash-save-error"]'));
    assert.equal(toggle().getAttribute('aria-checked'), 'true', 'the switch shows the state that is still saved');
  } finally {
    await view.unmount();
  }
});

test('Extensions settings shows localized plugin names in the interface language', async () => {
  const services = settingsRequest(() => ({ customized: false, providers: [
    { package_id: 'pkg.l', title: { zh: '主题背景', en: 'Theme background' }, skippable: true, max_duration_ms: 9500,
      enabled: true },
  ] }));
  const previousLanguage = i18n.language;
  await act(async () => i18n.changeLanguage('zh'));
  const view = await mounted(createElement(SettingsServicesProvider, { isConnected: true, connectionState: 'ready',
    request: services.request }, createElement(SplashProviderSettings, { disabled: false })));
  const name = () => view.container.querySelector('[data-testid="settings-extensions-splash-item"]').textContent;
  try {
    assert.equal(name(), '主题背景');
    await act(async () => i18n.changeLanguage('en'));
    assert.equal(name(), 'Theme background');
    assert.equal(view.container.querySelector('[data-testid="settings-extensions-splash-up-btn"]').getAttribute('aria-label'),
      i18n.t('settingsPanel.extensions.splash.moveUp', { name: 'Theme background' }));
  } finally {
    await view.unmount();
    await act(async () => i18n.changeLanguage(previousLanguage));
  }
});

test('Extensions settings shows its empty and disconnected states', async () => {
  const offline = settingsRequest(() => ({ providers: [], customized: false }));
  let view = await mounted(createElement(SettingsServicesProvider, { isConnected: false, connectionState: 'closed',
    request: offline.request }, createElement(SplashProviderSettings, { disabled: false })));
  assert.equal(view.container.querySelector('[data-testid="settings-extensions-splash-status"]').dataset.variant, 'disconnected');
  assert.deepEqual(offline.calls, []);
  await view.unmount();
  const empty = settingsRequest(() => ({ providers: [], customized: false }));
  view = await mounted(createElement(SettingsServicesProvider, { isConnected: true, connectionState: 'ready',
    request: empty.request }, createElement(SplashProviderSettings, { disabled: false })));
  assert.ok(view.container.querySelector('[data-testid="settings-extensions-splash-empty"]'));
  await view.unmount();
});

test('the Extensions module keeps only host features', () => {
  assert.deepEqual(extensionsModule.sections.map((section) => section.id), ['extensions-splash']);
});

// ── Splash preview (Gateway F_14) ────────────────────────────────────────────

test('splash preview plays one provider after the gate opened and leaves launch records alone', async () => {
  const app = splashSetup({ answers: { 'pkg.a': 'play' }, launch: splashLaunch(true) });
  await app.controller.start();
  assert.equal(app.controller.getState().phase, 'open');
  const marks = app.launch.marks;
  window.localStorage.removeItem('workswarm.splash.played');
  const provider = splashModule.parseSplashPlay({ package_id: 'pkg.a', skippable: true, max_duration_ms: 9500 });
  assert.deepEqual(provider, { packageId: 'pkg.a', skippable: true, maxDurationMs: 9500 });
  for (const bad of [{ package_id: '' }, { package_id: 'pkg.a', skippable: 'yes', max_duration_ms: 1 }, null]) {
    assert.equal(splashModule.parseSplashPlay(bad), null);
  }
  try {
    const done = app.controller.preview(provider);
    await settle();
    assert.equal(app.controller.getState().phase, 'playing');
    assert.deepEqual(app.readies(), ['pkg.a']);
    assert.deepEqual(app.transport.dispatches[0].payload,
      { launch_id: 'launch-1', first_visit: false, reduced_motion: false, preview: true });
    assert.equal(await app.controller.preview(provider), false, 'a second preview waits for none and is ignored');
    app.controller.surfaceEvent('ready');
    app.controller.skip();
    assert.equal(await done, true);
    assert.deepEqual(app.closes().map(([packageId, reason]) => [packageId, reason]), [['pkg.a', 'skip']]);
    assert.equal(app.controller.getState().phase, 'open');
    assert.equal(app.launch.marks, marks, 'the launch is not completed again');
    assert.equal(window.localStorage.getItem('workswarm.splash.played'), null, 'the device record is unchanged');
  } finally {
    app.runtime.dispose();
  }
});

test('splash preview is ignored while the startup queue still runs', async () => {
  const app = splashSetup({ answers: { 'pkg.a': 'hang' }, providers: providerList(['pkg.a']) });
  const started = app.controller.start();
  await settle();
  assert.equal(app.controller.getState().phase, 'preparing');
  assert.equal(await app.controller.preview({ packageId: 'pkg.b', skippable: true, maxDurationMs: 9500 }), false);
  assert.deepEqual(app.readies(), ['pkg.a']);
  app.controller.dispose();
  app.runtime.dispose();
  await Promise.race([started, settle()]);
});

test('WebUISplashGate plays a requested preview over the open workspace', async () => {
  const transport = splashTransport({ 'pkg.theme': 'play' });
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const rootElement = document.createElement('div'); rootElement.id = 'root'; document.body.append(rootElement);
  const reactRoot = createRoot(rootElement);
  const listeners = new Set();
  const subscribePreview = (listener) => { listeners.add(listener); return () => listeners.delete(listener); };
  const overlay = () => document.querySelector('[data-testid="webui-splash"]');
  try {
    await act(async () => reactRoot.render(createElement(WebUIProvider, { runtime, registry: componentRendererRegistry },
      createElement(WebUISplashGate, { launch: splashLaunch(true), hint: async () => 'none',
        requestProviders: async () => providerList(), subscribePreview },
        createElement('main', { 'data-testid': 'workspace' }, 'Workspace')))));
    await act(async () => settle());
    assert.equal(overlay(), null);
    assert.equal(rootElement.hasAttribute('inert'), false);
    assert.equal(listeners.size, 1);

    await act(async () => { for (const listener of listeners) listener({ package_id: 'pkg.theme', skippable: true, max_duration_ms: 9500 }); });
    await act(async () => settle());
    assert.equal(overlay().dataset.variant, 'playing');
    assert.equal(rootElement.hasAttribute('inert'), true, 'the workspace takes no input during the preview');
    const frame = overlay().querySelector('[data-testid="webui-media-surface-frame"]');
    const token = surfaceToken(frame);
    await postSurface(frame.contentWindow, surfaceMessage(token, 'ready'));
    await postSurface(frame.contentWindow, surfaceMessage(token, 'complete'));
    await act(async () => settle());
    assert.equal(overlay(), null);
    assert.equal(rootElement.hasAttribute('inert'), false);
    const closed = transport.dispatches.filter((request) => request.event === 'ui.closed');
    assert.deepEqual(closed.map((request) => [request.target.packageId, request.payload.reason]), [['pkg.theme', 'complete']]);
  } finally {
    await act(async () => reactRoot.unmount());
    runtime.dispose();
    rootElement.remove();
  }
});
