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
const { theme } = await import('antd');
const {
  componentCatalog, componentRendererRegistry, createProtocolPipeline, WebUISchemaRenderer,
  createWebUIRuntime, createWebUIAppRuntime, createMockTransport, WebUIHost, WebUIProvider, WebUIThemeProvider,
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
async function changeInput(input, value, isComposing = false) {
  await act(async () => {
    Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new window.InputEvent('input', { bubbles: true, inputType: 'insertText', isComposing }));
  });
}
async function pressEnter(input, options = {}) {
  await act(async () => {
    const init = { key: 'Enter', keyCode: 13, bubbles: true, ...options };
    input.dispatchEvent(new window.KeyboardEvent('keydown', init));
    input.dispatchEvent(new window.KeyboardEvent('keyup', init));
  });
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

const inputEvents = { 'ui.change': { id: 'edit', args: { field: 'city' } } };
const inputTree = (props = {}, bindings = inputEvents) => ({ component: 'Input', props, events: bindings });

test('Input sends exact change data and clear values without dispatching on mount, click, Enter or blur', async () => {
  const app = await setup(inputTree({ value: '北京', ariaLabel: 'City', allowClear: true, showCount: true, maxLength: 20 }));
  try {
    const input = app.container.querySelector('[data-testid="webui-input"]');
    assert.equal(input.value, '北京');
    assert.equal(input.getAttribute('aria-label'), 'City');
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
  const bindings = eventName === 'ui.change' ? inputEvents : inputSubmitEvents;
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
for (const eventName of ['ui.change', 'ui.press_enter']) {
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
