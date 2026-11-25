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

const { act, createElement: h, useId, useState, useEffect } = await import('react');
const { createRoot } = await import('react-dom/client');
const { ExpandedPanel, i18n, useWebUIPluginPanelTabs } = await import('../node_modules/.cache/panel-pages/components.mjs');
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
  function Harness({ names = ['a', 'b'], title = '', activeTab = 'planning', showMiddleTab = true, openRequest,
    sessionId = 'session', discoveredPanels, hiddenPanels }) {
    const slotId = useId();
    const [selection, setSelection] = useState(activeTab);
    const [closedBrowserSessions, setClosedBrowserSessions] = useState([]);
    const showBrowserTab = !closedBrowserSessions.includes(sessionId);
    const pluginTabs = useWebUIPluginPanelTabs({
      slotId,
      sessionId,
      panels: discoveredPanels ?? [],
      hiddenPanels: hiddenPanels ?? [],
      renderPanel: (key) => h(Body, { key, name: key }),
    });
    useEffect(() => setSelection(activeTab), [activeTab]);
    useEffect(() => {
      if (openRequest) setSelection(openRequest.key);
    }, [openRequest]);
    return h(ExpandedPanel, {
      activeTab: pluginTabs.activeTabKey ?? selection,
      onTabChange(key) {
        if (pluginTabs.selectTab(key)) return;
        pluginTabs.selectNativeTab();
        setSelection(key);
      },
      onTabClose(key) {
        if (pluginTabs.closeTab(key)) return;
        closed.push(key);
        setClosedBrowserSessions((sessions) => [...sessions, sessionId]);
      },
      addTab: discoveredPanels && { label: 'New tab', onOpen: pluginTabs.openDraft },
      additionalPanels: discoveredPanels ? pluginTabs.tabs : names.map((name) => ({
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
    async addPlugin(panelId) {
      await click(container.querySelector('[data-testid="tool-panel-tab-add"]'));
      await click(container.querySelector(`[data-testid="webui-panel-home-item"][data-variant="${panelId}"]`));
    },
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

const usagePanelId = 'pkg.stats|usage';
const filesPanelId = 'pkg.files|files';
const usagePanel = (sessionId) => ({ panelId: usagePanelId, key: `${sessionId}-usage`, title: 'Usage' });
const filesPanel = (sessionId) => ({ panelId: filesPanelId, key: `${sessionId}-files`, title: 'Files' });
const usageTabKey = `webui:plugin:${usagePanelId}`;
const filesTabKey = `webui:plugin:${filesPanelId}`;

test('clearing a plugin in the next session preserves tabs whose panels have not rendered yet', async () => {
  const app = await setup({ sessionId: 'a', discoveredPanels: [usagePanel('a'), filesPanel('a')] });
  try {
    await app.addPlugin(filesPanelId);
    await app.addPlugin(usagePanelId);
    await app.render({ sessionId: 'b', discoveredPanels: [] });
    await app.render({ sessionId: 'b', discoveredPanels: [filesPanel('b')] });
    assert.ok(app.tab(filesTabKey));
    await app.render({ sessionId: 'b', discoveredPanels: [] });
    assert.equal(app.tab(filesTabKey), null);
    await app.render({ sessionId: 'b', discoveredPanels: [usagePanel('b'), filesPanel('b')] });
    assert.equal(app.tab(filesTabKey), null);
    assert.equal(app.active(), usageTabKey);
    assert.equal(app.content(), 'Plugin b-usage');
  } finally {
    await app.dispose();
  }
});

test('a visibility preference round-trip restores the plugin tab without re-selecting it', async () => {
  const app = await setup({ sessionId: 'a', discoveredPanels: [usagePanel('a')] });
  try {
    await app.addPlugin(usagePanelId);
    assert.ok(app.tab(usageTabKey));
    assert.equal(app.active(), usageTabKey);
    // The instance-visibility preference turns off: the panel leaves the visible discovery
    // result but is still mounted in the scope, so the tab is parked, not pruned.
    await app.render({ sessionId: 'a', discoveredPanels: [], hiddenPanels: [usagePanel('a')] });
    assert.equal(app.tab(usageTabKey), null, 'a preference-hidden tab does not render');
    assert.equal(app.active(), 'planning', 'hiding the active tab falls back to the native page');
    // The preference turns back on: the tab returns in its original position and stays unselected.
    await app.render({ sessionId: 'a', discoveredPanels: [usagePanel('a')] });
    assert.ok(app.tab(usageTabKey), 're-enabling the preference restores the parked tab');
    assert.equal(app.active(), 'planning', 'the restored tab is not auto-selected');
    assert.equal(app.content(), 'Native tasks');
    await app.click(app.tab(usageTabKey));
    assert.equal(app.active(), usageTabKey);
    assert.equal(app.content(), 'Plugin a-usage');
  } finally {
    await app.dispose();
  }
});

test('pruning still removes tabs whose contribution left while its preference was off', async () => {
  const app = await setup({ sessionId: 'a', discoveredPanels: [usagePanel('a')] });
  try {
    await app.addPlugin(usagePanelId);
    await app.render({ sessionId: 'a', discoveredPanels: [], hiddenPanels: [usagePanel('a')] });
    assert.equal(app.tab(usageTabKey), null);
    // The contribution is cleared while hidden: it is in neither discovery list, so the tab is
    // permanently pruned and a later re-registration only comes back through a fresh choice.
    await app.render({ sessionId: 'a', discoveredPanels: [], hiddenPanels: [] });
    await app.render({ sessionId: 'a', discoveredPanels: [usagePanel('a')] });
    assert.equal(app.tab(usageTabKey), null, 'a cleared contribution does not resurrect its tab');
  } finally {
    await app.dispose();
  }
});

test('a parked tab stays unselected when the conversation switches with the preference already off', async () => {
  const app = await setup({ sessionId: 'a', discoveredPanels: [usagePanel('a')] });
  try {
    await app.addPlugin(usagePanelId);
    assert.equal(app.active(), usageTabKey);
    // The conversation switch rebuilds the discovery baseline, so the hidden panel is only known
    // from the new result: it must still hand the content area back to the native page.
    await app.render({ sessionId: 'b', discoveredPanels: [], hiddenPanels: [usagePanel('b')] });
    assert.equal(app.tab(usageTabKey), null, 'a preference-hidden tab does not render');
    assert.equal(app.active(), 'planning', 'a pre-hidden active tab falls back to the native page');
    // Re-enabling the preference restores the parked tab without taking the selection back.
    await app.render({ sessionId: 'b', discoveredPanels: [usagePanel('b')] });
    assert.ok(app.tab(usageTabKey), 're-enabling the preference restores the parked tab');
    assert.equal(app.active(), 'planning', 'the restored tab is not auto-selected');
  } finally {
    await app.dispose();
  }
});

test('closing an active draft skips neighbours that are still waiting to render', async () => {
  const app = await setup({ sessionId: 'a', discoveredPanels: [usagePanel('a'), filesPanel('a')] });
  try {
    await app.addPlugin(usagePanelId);
    await app.addPlugin(filesPanelId);
    await app.render({ sessionId: 'b', discoveredPanels: [usagePanel('b')] });
    await app.click(app.container.querySelector('[data-testid="tool-panel-tab-add"]'));
    await app.click(app.container.querySelector('[role="tab"][aria-selected="true"] [data-testid="tool-panel-tab-close"]'));
    assert.equal(app.active(), usageTabKey);
    await app.render({ sessionId: 'b', discoveredPanels: [usagePanel('b'), filesPanel('b')] });
    assert.ok(app.tab(filesTabKey));
    assert.equal(app.active(), usageTabKey);
  } finally {
    await app.dispose();
  }
});

test('closing the browser tab takes effect before switching sessions', async () => {
  const prototype = window.HTMLElement.prototype;
  const width = Object.getOwnPropertyDescriptor(prototype, 'offsetWidth');
  Object.defineProperty(prototype, 'offsetWidth', { configurable: true, get: () => 100 });
  const app = await setup({ sessionId: 'a', activeTab: 'browser' });
  try {
    await app.click(app.tab('browser').querySelector('[data-testid="tool-panel-tab-close"]'));
    assert.deepEqual(app.closed, ['browser']);
    assert.equal(app.tab('browser'), null);
    await app.render({ sessionId: 'b', activeTab: 'browser' });
    assert.equal(app.active(), 'browser');
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 160)); });
    assert.equal(app.active(), 'browser');
    assert.deepEqual(app.closed, ['browser']);
  } finally {
    await app.dispose();
    Object.defineProperty(prototype, 'offsetWidth', width);
  }
});

test('opening a new tab aligns it once without a delayed second scroll', async () => {
  const prototype = window.HTMLElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(prototype, 'scrollIntoView');
  const calls = [];
  Object.defineProperty(prototype, 'scrollIntoView', {
    configurable: true,
    value(options) { calls.push({ key: this.dataset.variant, options }); },
  });
  let app;
  try {
    app = await setup();
    calls.length = 0;
    await app.render({ names: ['a', 'b', 'c'], openRequest: { key: 'webui:c' } });
    assert.equal(app.active(), 'webui:c');
    assert.deepEqual(calls.filter((call) => call.key === 'webui:c'), [{
      key: 'webui:c', options: { behavior: 'auto', block: 'nearest', inline: 'nearest' },
    }]);
    const alignmentCount = calls.length;
    await act(async () => { await new Promise((resolve) => window.setTimeout(resolve, 250)); });
    assert.equal(calls.length, alignmentCount, 'the entrance animation must not trigger another scroll');
    await app.click(app.tab('planning'));
    assert.equal(calls.at(-1).key, 'planning');
    assert.equal(calls.length, alignmentCount + 1);
  } finally {
    if (app) await app.dispose();
    if (descriptor) Object.defineProperty(prototype, 'scrollIntoView', descriptor);
    else delete prototype.scrollIntoView;
  }
});

test('updating tab labels preserves the manually scrolled position', async () => {
  const prototype = window.HTMLElement.prototype;
  const descriptor = Object.getOwnPropertyDescriptor(prototype, 'scrollIntoView');
  let alignmentCount = 0;
  Object.defineProperty(prototype, 'scrollIntoView', {
    configurable: true,
    value() { alignmentCount += 1; },
  });
  let app;
  try {
    app = await setup({ activeTab: 'webui:b' });
    const list = app.container.querySelector('[role="tablist"]');
    list.scrollLeft = 145;
    const countBeforeUpdate = alignmentCount;
    await app.render({ activeTab: 'webui:b', title: ' updated' });
    assert.equal(app.active(), 'webui:b');
    assert.equal(list.scrollLeft, 145);
    assert.equal(alignmentCount, countBeforeUpdate);
  } finally {
    if (app) await app.dispose();
    if (descriptor) Object.defineProperty(prototype, 'scrollIntoView', descriptor);
    else delete prototype.scrollIntoView;
  }
});
