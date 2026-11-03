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
const { ExpandedPanel, BUILTIN_PANEL_KEY, usePanelPages, i18n } =
  await import('../node_modules/.cache/panel-pages/components.mjs');
await i18n.changeLanguage('en');

async function setup(initial = {}) {
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const unmounted = [];
  function Body({ name }) {
    useEffect(() => () => { unmounted.push(name); }, [name]);
    return h('div', null, `Plugin ${name}`);
  }
  function Harness({ scope = 's1', names = ['a', 'b', 'c'], title = '', onSectionTabClose }) {
    const [nativeTab, setNativeTab] = useState('planning');
    const pages = names.map((name) => ({
      key: `${scope}:${name}`, label: `${name}${title}`, content: h(Body, { key: name, name }),
    }));
    const { visiblePages, activePage, selectPage, closePage } = usePanelPages(pages, scope);
    return h(ExpandedPanel, {
      activeTab: nativeTab, onTabChange: setNativeTab,
      activePage, onPageChange: selectPage, onTabClose: closePage, onSectionTabClose,
      additionalPanels: visiblePages, onCollapse() {},
      middleTab: { key: 'team', label: 'Team', icon: null }, showMiddleTab: true, showBrowserTab: true,
      reviewPanel: h('div', null, 'Native review'), resolveActiveTab: (key) => key,
      renderPlanningContent: () => h('div', null, 'Native tasks'),
      renderMiddleTabContent: () => h('div', null, 'Native team'),
      renderBrowserContent: () => h('div', null, 'Native browser'),
    });
  }
  const render = async (props) => act(async () => root.render(h(Harness, props)));
  await render(initial);
  const tab = (key, builtin = false) => container.querySelector(
    `[data-testid="tool-panel-${builtin ? 'builtin-' : ''}tab"][data-variant="${key}"]`,
  );
  const close = (key) => tab(key).parentElement.querySelector('[data-testid="tool-panel-tab-close"]');
  const click = async (node) => {
    assert.ok(node);
    await act(async () => node.click());
  };
  return {
    container, tab, close, click, render, unmounted,
    active: () => container.querySelector('[data-testid="tool-panel-tab"][aria-selected="true"]').dataset.variant,
    async dispose() { await act(async () => root.unmount()); container.remove(); },
  };
}

test('native tools occupy one permanent outer page with functional nonclosable sections', async () => {
  const app = await setup();
  try {
    assert.equal(app.active(), BUILTIN_PANEL_KEY);
    assert.equal(app.close(BUILTIN_PANEL_KEY), null);
    assert.equal(app.container.querySelectorAll('[data-testid="tool-panel-tab"]').length, 4);
    assert.equal(app.container.querySelectorAll('[data-testid="tool-panel-builtin-tab-close"]').length, 0);
    for (const [key, text] of [
      ['team', 'Native team'], ['artifacts', 'Native artifacts'], ['review', 'Native review'],
      ['browser', 'Native browser'], ['organization', 'Native organization'], ['planning', 'Native tasks'],
    ]) {
      await app.click(app.tab(key, true));
      assert.equal(app.container.querySelector('[data-testid="tool-panel-builtin-content"]').textContent, text);
    }
    await app.click(app.tab('s1:a'));
    assert.equal(app.container.querySelector('[data-testid="tool-panel-builtin-expanded-header"]'), null);
    await app.click(app.tab(BUILTIN_PANEL_KEY));
    assert.match(app.container.textContent, /Native tasks/);
  } finally { await app.dispose(); }
});

test('closing inactive pages preserves selection; closing active pages selects a neighbor and unmounts content', async () => {
  const app = await setup();
  try {
    await app.click(app.tab('s1:b'));
    assert.equal(app.tab('s1:b').tagName, 'BUTTON');
    assert.equal(app.close('s1:b').closest('[role="tab"]'), null, 'close is a sibling, not a nested button');
    await app.click(app.close('s1:a'));
    assert.equal(app.active(), 's1:b');
    await app.click(app.close('s1:b'));
    assert.equal(app.active(), 's1:c');
    assert.ok(app.unmounted.includes('b'));
    await app.click(app.close('s1:c'));
    assert.equal(app.active(), BUILTIN_PANEL_KEY);
    assert.match(app.container.textContent, /Native tasks/);
  } finally { await app.dispose(); }
});

test('closing the last active page selects its left neighbor', async () => {
  const app = await setup();
  try {
    await app.click(app.tab('s1:c'));
    await app.click(app.close('s1:c'));
    assert.equal(app.active(), 's1:b');
  } finally { await app.dispose(); }
});

test('dismissed instances stay closed on plugin updates and are isolated by session', async () => {
  const app = await setup();
  try {
    await app.click(app.close('s1:a'));
    await app.render({ title: ' updated' });
    assert.equal(app.tab('s1:a'), null);
    assert.equal(app.tab('s1:b').textContent, 'b updated');
    await app.click(app.tab('s1:b'));
    await app.render({ scope: 's2' });
    assert.equal(app.active(), BUILTIN_PANEL_KEY);
    assert.ok(app.tab('s2:a'));
    await app.render({ scope: 's1' });
    assert.equal(app.tab('s1:a'), null);
    assert.equal(app.active(), BUILTIN_PANEL_KEY);
  } finally { await app.dispose(); }
});

test('plugin clear removes the selected page and falls back without reviving stale selection', async () => {
  const app = await setup();
  try {
    await app.click(app.tab('s1:b'));
    await app.render({ names: ['a', 'c'] });
    assert.equal(app.active(), BUILTIN_PANEL_KEY);
    assert.ok(app.unmounted.includes('b'));
    await app.render({});
    assert.equal(app.active(), BUILTIN_PANEL_KEY);
  } finally { await app.dispose(); }
});

test('browser section keeps its close entry inside the builtin tools page', async () => {
  const closed = [];
  const app = await setup({ onSectionTabClose: (key) => closed.push(key) });
  try {
    const closeButton = app.tab('browser', true).parentElement
      .querySelector('[data-testid="tool-panel-builtin-tab-close"]');
    assert.ok(closeButton, 'browser section renders its close button');
    await app.click(closeButton);
    assert.deepEqual(closed, ['browser']);
  } finally { await app.dispose(); }
});

test('keyboard navigation selects pages and close returns focus to a surviving page', async () => {
  const app = await setup();
  try {
    const press = async (key) => act(async () => {
      app.tab(app.active()).dispatchEvent(new window.KeyboardEvent('keydown', { key, bubbles: true }));
    });
    await press('ArrowRight');
    assert.equal(app.active(), 's1:a');
    assert.equal(document.activeElement, app.tab('s1:a'));
    await press('End');
    assert.equal(app.active(), 's1:c');
    await press('Home');
    assert.equal(app.active(), BUILTIN_PANEL_KEY);
    await app.click(app.tab('s1:a'));
    await app.click(app.close('s1:a'));
    assert.equal(document.activeElement, app.tab('s1:b'));
    const panel = app.container.querySelector('[data-testid="tool-panel-expanded-content"]');
    assert.equal(panel.getAttribute('aria-labelledby'), app.tab(app.active()).id);
  } finally { await app.dispose(); }
});
