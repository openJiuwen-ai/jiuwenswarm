import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost/' });
const previous = new Map();
for (const [name, value] of Object.entries({
  window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
}
after(() => {
  dom.window.close();
  for (const [name, descriptor] of previous) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

const { act, createElement: h, useState, useEffect } = await import('react');
const { createRoot } = await import('react-dom/client');
const { ExpandedPanel, i18n } = await import('../node_modules/.cache/panel-pages/components.mjs');
await i18n.changeLanguage('en');

async function setup(initial = {}) {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const unmounted = [];
  const closed = [];
  function Body({ name }) {
    useEffect(() => () => { unmounted.push(name); }, [name]);
    return h('div', null, `Plugin ${name}`);
  }
  function Harness({ names = ['a', 'b'], title = '', activeTab = 'planning', showMiddleTab = true, openRequest }) {
    const [selection, setSelection] = useState(activeTab);
    const [showBrowserTab, setShowBrowserTab] = useState(true);
    useEffect(() => setSelection(activeTab), [activeTab]);
    useEffect(() => {
      if (openRequest) setSelection(openRequest.key);
    }, [openRequest]);
    return h(ExpandedPanel, {
      activeTab: selection, onTabChange: setSelection,
      onTabClose(key) { closed.push(key); setShowBrowserTab(false); },
      additionalPanels: names.map((name) => ({
        key: `webui:${name}`, label: `${name}${title}`, content: h(Body, { key: name, name }),
      })),
      onCollapse() {},
      middleTab: { key: 'team', label: 'Team', icon: null }, showMiddleTab, showBrowserTab,
      reviewPanel: h('div', null, 'Native review'),
      resolveActiveTab: (key) => ['planning', 'team', 'artifacts', 'review'].includes(key)
        || (key === 'browser' && showBrowserTab) ? key : 'planning',
      renderPlanningContent: () => h('div', null, 'Native tasks'),
      renderMiddleTabContent: () => h('div', null, 'Native team'),
      renderBrowserContent: () => h('div', null, 'Native browser'),
    });
  }
  const render = async (props) => act(async () => root.render(h(Harness, props)));
  await render(initial);
  const tab = (key) => container.querySelector(`[data-testid="tool-panel-tab"][data-variant="${key}"]`);
  const click = async (node) => {
    assert.ok(node);
    await act(async () => node.click());
  };
  return {
    container, tab, click, render, unmounted, closed,
    active: () => container.querySelector('[role="tab"][aria-selected="true"]').dataset.variant,
    content: () => container.querySelector('[data-testid="tool-panel-expanded-content"]').textContent,
    async dispose() { await act(async () => root.unmount()); container.remove(); },
  };
}

test('native functions and plugin pages share the original single tab row', async () => {
  const app = await setup();
  try {
    assert.equal(app.container.querySelectorAll('[role="tablist"]').length, 1);
    assert.equal(app.container.querySelectorAll('[role="tabpanel"]').length, 1);
    assert.deepEqual([...app.container.querySelectorAll('[role="tab"]')].map((node) => node.dataset.variant),
      ['planning', 'team', 'artifacts', 'review', 'browser', 'organization', 'webui:a', 'webui:b']);
    assert.equal(app.tab('builtin-tools'), null);
    for (const [key, text] of [
      ['team', 'Native team'], ['artifacts', 'Native artifacts'], ['review', 'Native review'],
      ['browser', 'Native browser'], ['organization', 'Native organization'],
      ['webui:a', 'Plugin a'], ['webui:b', 'Plugin b'], ['planning', 'Native tasks'],
    ]) {
      await app.click(app.tab(key));
      assert.equal(app.active(), key);
      assert.equal(app.content(), text);
      const panel = app.container.querySelector('[role="tabpanel"]');
      assert.equal(panel.getAttribute('aria-labelledby'), app.tab(key).id);
    }
    assert.ok(app.unmounted.includes('a'));
  } finally { await app.dispose(); }
});

test('only the browser retains its original close button', async () => {
  const app = await setup();
  try {
    const buttons = app.container.querySelectorAll('[data-testid="tool-panel-tab-close"]');
    assert.equal(buttons.length, 1);
    assert.equal(buttons[0].closest('[role="tab"]').dataset.variant, 'browser');
    await app.click(app.tab('webui:a'));
    await app.click(buttons[0]);
    assert.deepEqual(app.closed, ['browser']);
    assert.equal(app.tab('browser'), null);
    assert.equal(app.active(), 'webui:a');
    assert.equal(app.content(), 'Plugin a');
  } finally { await app.dispose(); }
});

test('closing the active browser falls back to tasks', async () => {
  const app = await setup();
  try {
    await app.click(app.tab('browser'));
    await app.click(app.tab('browser').querySelector('[data-testid="tool-panel-tab-close"]'));
    assert.equal(app.active(), 'planning');
    assert.equal(app.content(), 'Native tasks');
  } finally { await app.dispose(); }
});

test('plugin updates preserve selection and clear removes the page', async () => {
  const app = await setup();
  try {
    await app.click(app.tab('webui:a'));
    await app.render({ title: ' updated' });
    assert.equal(app.active(), 'webui:a');
    assert.equal(app.tab('webui:a').textContent, 'a updated');
    await app.render({ names: ['b'] });
    assert.equal(app.tab('webui:a'), null);
    assert.equal(app.active(), 'planning');
    assert.ok(app.unmounted.includes('a'));
  } finally { await app.dispose(); }
});

test('external native selection replaces plugin content', async () => {
  const app = await setup();
  try {
    await app.click(app.tab('webui:b'));
    await app.render({ activeTab: 'review' });
    assert.equal(app.active(), 'review');
    assert.equal(app.content(), 'Native review');
    await app.render({ activeTab: 'browser' });
    assert.equal(app.active(), 'browser');
    assert.equal(app.content(), 'Native browser');
  } finally { await app.dispose(); }
});

test('native tabs remain usable without plugins or optional members', async () => {
  const app = await setup({ names: [], showMiddleTab: false });
  try {
    assert.equal(app.active(), 'planning');
    assert.equal(app.tab('team'), null);
    assert.equal(app.container.querySelectorAll('[role="tablist"]').length, 1);
    await act(async () => app.tab('review').dispatchEvent(
      new window.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true }),
    ));
    assert.equal(app.active(), 'review');
    await act(async () => app.tab('planning').dispatchEvent(
      new window.KeyboardEvent('keydown', { key: ' ', bubbles: true, cancelable: true }),
    ));
    assert.equal(app.active(), 'planning');
  } finally { await app.dispose(); }
});

test('an explicit open request selects a plugin page without later updates taking selection back', async () => {
  const app = await setup();
  const openRequest = { key: 'webui:a' };
  try {
    await app.render({ openRequest });
    assert.equal(app.active(), 'webui:a');
    await app.click(app.tab('webui:b'));
    await app.render({ openRequest, title: ' next turn' });
    assert.equal(app.active(), 'webui:b');
  } finally {
    await app.dispose();
  }
});
