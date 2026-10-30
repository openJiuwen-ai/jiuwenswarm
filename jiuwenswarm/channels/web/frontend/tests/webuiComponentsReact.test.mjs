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
const previous = new Map();
const channels = [];
const NativeMessageChannel = globalThis.MessageChannel;
class ComponentMessageChannel extends NativeMessageChannel {
  constructor() { super(); channels.push(this); }
}
const noop = () => {};
dom.window.matchMedia = () => ({
  matches: false, addListener: noop, removeListener: noop, addEventListener: noop, removeEventListener: noop,
});
class ResizeObserver { observe() {} unobserve() {} disconnect() {} }
for (const [name, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  Element: dom.window.Element, HTMLElement: dom.window.HTMLElement, SVGElement: dom.window.SVGElement,
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
const { theme } = await import('antd');
const {
  componentCatalog, componentRendererRegistry, createProtocolPipeline, WebUISchemaRenderer,
  createWebUIRuntime, createMockTransport, WebUIHost, WebUIProvider, WebUIThemeProvider,
  WebUIContentActivity, resolveWebUITheme, i18n,
} = await import('../node_modules/.cache/webui-components/components.mjs');

const target = {
  session_id: 'sess-1', slot_id: 'conversation.input.above', package_id: 'pkg.demo', instance_key: 'main',
};
const frame = (root, revision = 1, scope = target) => ({
  target: scope, schema_version: 1, revision, mode: 'replace', root,
});
const events = { 'ui.click': { id: 'confirm', args: { source: 'toolbar' } } };
async function click(element) {
  assert.ok(element, 'click target must exist');
  await act(async () => element.dispatchEvent(new window.MouseEvent('click', { bubbles: true })));
}
async function setup(tree, scope = target) {
  const transport = createMockTransport();
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
  element.style.setProperty('--font-size-base', '15px');
  element.style.setProperty('--radius-md', '9px');
  const resolved = resolveWebUITheme(getComputedStyle(element), false);
  assert.equal(resolved.token.colorPrimary, '#345678');
  assert.equal(resolved.token.fontSize, 15);
  assert.equal(resolved.token.borderRadius, 9);
  assert.equal(resolved.token.colorError, undefined);
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
    });
    assert.equal(token.colorPrimary, '#765432');
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
    await app.replace({ component: 'Text', props: { value: 'Plain', max_lines: 0, monospace: false } });
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
    assert.equal(first.getAttribute('sandbox'), 'allow-scripts allow-forms');
    assert.equal(first.getAttribute('referrerpolicy'), 'no-referrer');
    assert.equal(first.title, 'Preview');
    assert.equal(state().dataset.variant, 'loading');
    await act(async () => first.dispatchEvent(new window.Event('load')));
    assert.ok(!state(), 'loaded frame hides its notice');
    await app.activity(false);
    assert.ok(!iframe(), 'inactive content releases its iframe'); assert.equal(state().dataset.variant, 'inactive');
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
    assert.equal(app.business().length, 0);
  } finally { await app.close(); }
});
