import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { JSDOM, VirtualConsole } from 'jsdom';
import { readFileSync } from 'node:fs';

// Install the browser before ReactDOM and the library inspect browser capabilities.
const dom = new JSDOM('<!doctype html><html><body></body></html>', {
  url: 'http://localhost/', pretendToBeVisual: true, virtualConsole: new VirtualConsole(),
});
const textStyles = dom.window.document.createElement('style');
textStyles.textContent = readFileSync(new URL(
  '../src/features/webui/renderer/adapters/webuiText/WebUIText.css', import.meta.url,
), 'utf8');
dom.window.document.head.append(textStyles);
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

const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');
const { renderToStaticMarkup } = await import('react-dom/server');
const { theme } = await import('antd');
const {
  componentCatalog, componentRendererRegistry, createProtocolPipeline, WebUISchemaRenderer, FileTree, createComponentState,
  createWebUIRuntime, createWebUIAppRuntime, createMockTransport, WebUIHost, WebUIEntryHost, WebUIProvider, WebUIThemeProvider,
  WebUISlotProvider, WebUIContentActivity, resolveWebUITheme, i18n, buttonIcon, browserClientCapabilities, GatewayImage,
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
async function setup(tree, scope = target, transportOptions = {}) {
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
  await act(async () => transport.injectRenderFrame(frame(tree, 1, scope)));
  return {
    container, runtime, transport,
    business: () => transport.dispatches.filter((request) => request.event !== 'ui.ready'),
    async replace(next, revision = 2) {
      await act(async () => transport.injectRenderFrame(frame(next, revision, scope)));
    },
    async activity(value) { active = value; await act(async () => render()); },
    async close() { await act(async () => root.unmount()); runtime.dispose(); container.remove(); },
  };
}

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
    await app.replace(tree({ url: '/logo.svg', children: 'T', alt: 'Account image', style }), 3);
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
  const app = await setup({ component: 'Button', props: { label: '确认', variant: 'primary', size: 'sm' }, events });
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

test('theme initialization preserves cached instances and mounts each host once', async () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  transport.injectRenderFrame(frame({ component: 'Button', props: { label: 'Cached' }, events }));
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
  const app = await setup({ component: 'Button', props: { label: '导出报告', variant: 'ghost' }, events });
  try {
    const button = app.container.querySelector('[data-testid="webui-button"]');
    assert.equal(button.textContent, '导出报告');
    assert.equal(button.querySelector('svg'), null);
    assert.match(button.className, /ant-btn-text/);
    await click(button);
    assert.equal(app.business().length, 1);
    await app.replace({ component: 'Button', props: { label: '删除记录', variant: 'danger' }, events });
    assert.equal(app.container.querySelector('button').textContent, '删除记录');
    assert.match(app.container.querySelector('button').className, /ant-btn-dangerous/);
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
      label: '本轮操作 / Round actions', icon, iconOnly: true, variant: 'ghost',
    }, events };
    const app = await setup(tree);
    try {
      const button = app.container.querySelector('[data-testid="webui-button"]');
      assert.equal(button.textContent, '');
      assert.ok(button.querySelector('svg'));
      assert.equal(button.getAttribute('aria-label'), tree.props.label);
      assert.equal(button.title, tree.props.label);
      assert.match(button.className, /ant-btn-text/);
      assert.match(button.className, /ant-btn-icon-only/);
      await click(button);
      assert.equal(app.business().length, 1);
      assert.equal(app.business()[0].payload.action_id, 'confirm');
      await app.replace({ ...tree, props: { ...tree.props, iconOnly: false } });
      assert.equal(app.container.querySelector('button').textContent, tree.props.label);
      await act(async () => app.transport.setConnectionState('disconnected'));
      assert.equal(app.container.querySelector('button').disabled, true);
    } finally { await app.close(); }
  });
}

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
  const app = await setup({ component: 'Button', props: { label: 'Disabled', disabled: true }, events });
  try {
    await click(app.container.querySelector('button'));
    assert.equal(app.business().length, 0);
    await app.replace({ component: 'Button', props: { label: 'Unbound', variant: 'ghost' } });
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
    component: 'Box', children: [{ component: 'Button', props: { label: 'Open' }, events }],
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

test('Text respects theme sizes, semantic tones and host style precedence', async () => {
  const container = document.createElement('div'); document.body.append(container);
  const root = createRoot(container);
  try {
    await act(async () => root.render(createElement(componentRendererRegistry.get('Text'), {
      node: { props: { value: 'Status', tone: 'danger', size: 'lg', weight: 'bold', monospace: true, max_lines: 2 } },
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

test('Text variants use semantic colors, explicit tone wins and secondary Button is filled', async () => {
  const app = await setup({ component: 'Text', props: { value: 'Status', variant: 'primary' } });
  const text = () => app.container.querySelector('[data-testid="webui-text"]');
  try {
    const primary = text().style.color;
    assert.ok(primary);
    await app.replace({ component: 'Text', props: { value: 'Status', variant: 'ghost' } }, 2);
    assert.ok(text().style.color);
    assert.notEqual(text().style.color, primary);
    await app.replace({ component: 'Text', props: { value: 'Status', variant: 'danger' } }, 3);
    assert.match(text().className, /ant-typography-danger/);
    await app.replace({ component: 'Text', props: { value: 'Status', variant: 'danger', tone: 'neutral' } }, 4);
    assert.doesNotMatch(text().className, /ant-typography-danger/);
    assert.equal(text().style.color, '');
    await app.replace({ component: 'Text', props: { value: 'Status', variant: 'primary', tone: 'success' } }, 5);
    assert.match(text().className, /ant-typography-success/);
    assert.equal(text().style.color, '');
    await app.replace({ component: 'Button', props: { label: 'Secondary', variant: 'secondary', size: 'large' } }, 6);
    const button = app.container.querySelector('[data-testid="webui-button"]');
    assert.match(button.className, /ant-btn-variant-filled/);
    assert.match(button.className, /ant-btn-lg/);
    await app.replace({ component: 'Button', props: { label: 'Default', size: 'lg' } }, 7);
    assert.doesNotMatch(app.container.querySelector('button').className, /ant-btn-variant-filled/);
    assert.match(app.container.querySelector('button').className, /ant-btn-lg/);
  } finally { await app.close(); }
});

test('Text renders monospace, line limits and info tone from normalized props', async () => {
  const app = await setup({
    component: 'Text', props: { value: 'First\nSecond\nThird', monospace: true, max_lines: 2, tone: 'info' },
  });
  try {
    const text = app.container.querySelector('[data-testid="webui-text"]');
    assert.equal(text.style.fontFamily, 'var(--font-mono)');
    assert.equal(getComputedStyle(text).display, '-webkit-box');
    assert.equal(text.style.getPropertyValue('--webui-text-max-lines'), '2');
    assert.equal(getComputedStyle(text).overflow, 'hidden');
    assert.ok(text.style.color, 'info tone must have a semantic color');
    await app.replace({ component: 'Text', props: { value: 'Plain', monospace: false } });
    const plain = app.container.querySelector('[data-testid="webui-text"]');
    assert.equal(plain.style.fontFamily, '');
    assert.equal(plain.style.getPropertyValue('--webui-text-max-lines'), '');
    assert.ok(!plain.classList.contains('webui-text-clamped'));
    assert.equal(plain.style.color, '');
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
            { component: 'Avatar', props: { url: '/logo.svg', alt: 'Welcome', size: 64 } },
            {
              component: 'Button',
              props: { label: 'Get started', variant: 'primary' },
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
    { component: 'Button', props: { label: '确认', form: 'credentials' }, events },
    { component: 'Button', props: { label: '普通按钮' }, events },
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
    { component: 'Button', props: { label: 'First', form: 'one' }, events },
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
    children: [{ component: 'Avatar', props: { url: '/logo.svg', alt: 'User' } }] });
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
    await act(async () => transport.injectRenderFrame(frame({ component: 'Button', props: { label: 'Current view' }, events }, 1, next)));
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
