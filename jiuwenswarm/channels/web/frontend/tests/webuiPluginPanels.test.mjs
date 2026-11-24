/**
 * Right-workspace plugin tab state rules.
 *
 * A plus click creates a blank, plugin-agnostic tab; choosing a plugin converts that same tab in
 * place. Blank and plugin tabs share the same close behavior, added tabs stay across conversations
 * (and across panel unmounts through the scoped in-memory store), and pruning only removes plugin
 * contributions that actually disappeared for the currently rendered conversation.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

import {
  chooseWebUIPluginPanel,
  closeWebUIPluginPanelTab,
  EMPTY_WEBUI_PLUGIN_PANEL_TABS,
  hideWebUIPluginPanels,
  isWebUIPluginPanelDraftTab,
  openWebUIPluginPanelDraft,
  removeWebUIPluginPanels,
  selectNativeWebUIPanelTab,
  selectWebUIPluginPanelTab,
  webUIPluginPanelDraftTabId,
  webUIPluginPanelIdFromTabId,
  webUIPluginPanelTabId,
  webUIPluginPanelTabIdFromKey,
  webUIPluginPanelTabKey,
} from '../node_modules/.cache/webui-plugin-panels/pluginPanelTabs.js';
import {
  createWebUIPluginPanelTabsStore,
  getWebUIPluginPanelTabsStore,
} from '../node_modules/.cache/webui-plugin-panels/pluginPanelTabsStore.js';
import {
  openSingleAgentPanel,
  useSingleAgentPanelState,
} from '../node_modules/.cache/webui-plugin-panels/singleAgentPanelState.js';
import {
  openArtifactPanel,
  openTeamPanel,
  useTeamPanelState,
} from '../node_modules/.cache/webui-plugin-panels/teamPanelState.js';

const DRAFT_1 = webUIPluginPanelDraftTabId(1);
const DRAFT_2 = webUIPluginPanelDraftTabId(2);
const DRAFT_3 = webUIPluginPanelDraftTabId(3);
const PLUGIN_A = webUIPluginPanelTabId('pkg.a|files');
const PLUGIN_B = webUIPluginPanelTabId('pkg.b|preview');

test('the plus button creates a distinct blank tab and activates it', () => {
  const first = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  assert.deepEqual(first.tabs, [DRAFT_1]);
  assert.equal(first.activeId, DRAFT_1);
  assert.equal(first.draftSeq, 1);
  assert.equal(isWebUIPluginPanelDraftTab(DRAFT_1), true);
  assert.equal(webUIPluginPanelIdFromTabId(DRAFT_1), null);

  const second = openWebUIPluginPanelDraft(first);
  assert.deepEqual(second.tabs, [DRAFT_1, DRAFT_2]);
  assert.equal(second.activeId, DRAFT_2);
});

test('choosing a plugin converts the blank tab in place and activates it', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  assert.deepEqual(state.tabs, [PLUGIN_A]);
  assert.equal(state.activeId, PLUGIN_A);
  assert.equal(webUIPluginPanelIdFromTabId(PLUGIN_A), 'pkg.a|files');
});

test('choosing from one of two blank tabs only converts that tab', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = openWebUIPluginPanelDraft(state);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  assert.deepEqual(state.tabs, [PLUGIN_A, DRAFT_2]);
  assert.equal(state.activeId, PLUGIN_A);
});

test('choosing an already opened plugin drops the blank tab and activates the existing tab', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  state = openWebUIPluginPanelDraft(state);
  state = chooseWebUIPluginPanel(state, DRAFT_2, 'pkg.a|files');
  assert.deepEqual(state.tabs, [PLUGIN_A]);
  assert.equal(state.activeId, PLUGIN_A);
});

test('choosing a plugin after its blank tab disappeared appends a new tab', () => {
  const state = chooseWebUIPluginPanel(EMPTY_WEBUI_PLUGIN_PANEL_TABS, DRAFT_1, 'pkg.a|files');
  assert.deepEqual(state.tabs, [PLUGIN_A]);
  assert.equal(state.activeId, PLUGIN_A);
});

test('selecting an existing tab keeps the tab order', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  state = openWebUIPluginPanelDraft(state);
  state = chooseWebUIPluginPanel(state, DRAFT_2, 'pkg.b|preview');
  state = selectWebUIPluginPanelTab(state, PLUGIN_A);
  assert.deepEqual(state.tabs, [PLUGIN_A, PLUGIN_B]);
  assert.equal(state.activeId, PLUGIN_A);
  assert.equal(selectWebUIPluginPanelTab(state, 'plugin:unknown'), state);
});

test('closing the active blank tab falls back to the native page', () => {
  const state = closeWebUIPluginPanelTab(openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS), DRAFT_1, []);
  assert.deepEqual(state, { tabs: [], activeId: null, draftSeq: 1 });
});

test('closing the active tab activates the right neighbour until no tab is left', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  state = openWebUIPluginPanelDraft(state);
  state = chooseWebUIPluginPanel(state, DRAFT_2, 'pkg.b|preview');
  state = selectWebUIPluginPanelTab(state, PLUGIN_A);
  state = closeWebUIPluginPanelTab(state, PLUGIN_A, ['pkg.a|files', 'pkg.b|preview']);
  assert.deepEqual(state.tabs, [PLUGIN_B]);
  assert.equal(state.activeId, PLUGIN_B);

  state = closeWebUIPluginPanelTab(state, PLUGIN_B, ['pkg.a|files', 'pkg.b|preview']);
  assert.deepEqual(state, { tabs: [], activeId: null, draftSeq: 2 });
});

test('closing the last tab activates its left neighbour', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  state = openWebUIPluginPanelDraft(state);
  state = closeWebUIPluginPanelTab(state, DRAFT_2, ['pkg.a|files']);
  assert.deepEqual(state.tabs, [PLUGIN_A]);
  assert.equal(state.activeId, PLUGIN_A);
});

test('closing an inactive tab keeps the active page', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  state = openWebUIPluginPanelDraft(state);
  state = selectWebUIPluginPanelTab(state, PLUGIN_A);
  state = closeWebUIPluginPanelTab(state, DRAFT_2, ['pkg.a|files']);
  assert.deepEqual(state.tabs, [PLUGIN_A]);
  assert.equal(state.activeId, PLUGIN_A);
});

test('closing an unknown tab is a no-op', () => {
  const state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  assert.equal(closeWebUIPluginPanelTab(state, DRAFT_2, []), state);
});

test('a closed plugin can be chosen again from a new blank tab at the end', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  state = openWebUIPluginPanelDraft(state);
  state = chooseWebUIPluginPanel(state, DRAFT_2, 'pkg.b|preview');
  state = closeWebUIPluginPanelTab(state, PLUGIN_A, ['pkg.a|files', 'pkg.b|preview']);
  state = openWebUIPluginPanelDraft(state);
  state = chooseWebUIPluginPanel(state, DRAFT_3, 'pkg.a|files');
  assert.deepEqual(state.tabs, [PLUGIN_B, PLUGIN_A]);
  assert.equal(state.activeId, PLUGIN_A);
});

test('selecting a native tab leaves the plugin content area', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = selectNativeWebUIPanelTab(state);
  assert.equal(state.activeId, null);
  assert.equal(selectNativeWebUIPanelTab(state), state);
});

test('pruning drops plugin tabs whose contribution disappeared and keeps blank tabs', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  state = openWebUIPluginPanelDraft(state);
  state = chooseWebUIPluginPanel(state, DRAFT_2, 'pkg.b|preview');
  state = openWebUIPluginPanelDraft(state);
  state = removeWebUIPluginPanels(state, ['pkg.b|preview']);
  assert.deepEqual(state.tabs, [PLUGIN_A, DRAFT_3]);
});

test('hiding keeps the parked tab and drops only its activation', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  state = openWebUIPluginPanelDraft(state);
  state = chooseWebUIPluginPanel(state, DRAFT_2, 'pkg.b|preview');
  // An inactive tab can be hidden without touching the current selection.
  const inactiveHidden = hideWebUIPluginPanels(state, ['pkg.a|files']);
  assert.deepEqual(inactiveHidden.tabs, state.tabs);
  assert.equal(inactiveHidden.activeId, PLUGIN_B);
  assert.equal(hideWebUIPluginPanels(inactiveHidden, ['pkg.a|files']), inactiveHidden,
    'hiding an already hidden tab is a no-op');
  // Hiding the active tab hands the content area to the native page; the tab id stays parked so
  // the tab bar renders it again once the preference is back on.
  const hidden = hideWebUIPluginPanels(inactiveHidden, ['pkg.b|preview']);
  assert.deepEqual(hidden.tabs, inactiveHidden.tabs);
  assert.equal(hidden.activeId, null, 'hiding the active tab hands the content area to the native page');
  assert.equal(hideWebUIPluginPanels(hidden, ['pkg.unknown|none']), hidden,
    'hiding an untracked panel is a no-op');
});

test('pruning the active plugin tab falls back to the native page', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  state = removeWebUIPluginPanels(state, ['pkg.a|files']);
  assert.deepEqual(state, { tabs: [], activeId: null, draftSeq: 1 });
});

test('removing no tracked panels returns the same state reference', () => {
  let state = openWebUIPluginPanelDraft(EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  state = chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files');
  assert.equal(removeWebUIPluginPanels(state, ['pkg.b|preview']), state);
  assert.equal(removeWebUIPluginPanels(EMPTY_WEBUI_PLUGIN_PANEL_TABS, []), EMPTY_WEBUI_PLUGIN_PANEL_TABS);
});

test('tab keys and ids round-trip for plugin and blank tabs', () => {
  assert.equal(webUIPluginPanelTabKey(PLUGIN_A), `webui:${PLUGIN_A}`);
  assert.equal(webUIPluginPanelTabIdFromKey(`webui:${PLUGIN_A}`), PLUGIN_A);
  assert.equal(webUIPluginPanelTabKey(DRAFT_1), `webui:${DRAFT_1}`);
  assert.equal(webUIPluginPanelTabIdFromKey(`webui:${DRAFT_1}`), DRAFT_1);
  assert.equal(webUIPluginPanelTabIdFromKey('planning'), null);
  assert.equal(webUIPluginPanelIdFromTabId(PLUGIN_A), 'pkg.a|files');
});

test('a scoped store keeps tabs across panel unmounts and notifies only on change', () => {
  const store = createWebUIPluginPanelTabsStore();
  let notifications = 0;
  const unsubscribe = store.subscribe(() => {
    notifications += 1;
  });

  store.update(openWebUIPluginPanelDraft);
  assert.equal(notifications, 1);
  assert.equal(store.getState().activeId, DRAFT_1);

  store.update((current) => current);
  assert.equal(notifications, 1);

  store.update((current) => chooseWebUIPluginPanel(current, DRAFT_1, 'pkg.a|files'));
  assert.equal(notifications, 2);
  assert.deepEqual(store.getState().tabs, [PLUGIN_A]);

  unsubscribe();
  store.update((current) => closeWebUIPluginPanelTab(current, PLUGIN_A, ['pkg.a|files']));
  assert.equal(notifications, 2);

  store.reset();
  assert.deepEqual(store.getState(), EMPTY_WEBUI_PLUGIN_PANEL_TABS);
});

test('separate panel regions keep independent tab state', () => {
  const right = createWebUIPluginPanelTabsStore();
  const left = createWebUIPluginPanelTabsStore();
  right.update(openWebUIPluginPanelDraft);
  assert.deepEqual(left.getState(), EMPTY_WEBUI_PLUGIN_PANEL_TABS);
  right.reset();
});

function installPanelDom() {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const globals = {
    window: dom.window,
    document: dom.window.document,
    CustomEvent: dom.window.CustomEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const previous = new Map();
  for (const [name, value] of Object.entries(globals)) {
    previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  return () => {
    for (const [name, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    dom.window.close();
  };
}

function activatePluginPanel() {
  const store = getWebUIPluginPanelTabsStore('conversation.panel.right');
  store.reset();
  store.update(openWebUIPluginPanelDraft);
  store.update((state) => chooseWebUIPluginPanel(state, DRAFT_1, 'pkg.a|files'));
  store.update(openWebUIPluginPanelDraft);
  store.update((state) => selectWebUIPluginPanelTab(state, PLUGIN_A));
  return store;
}

for (const mode of [
  {
    name: 'single agent',
    usePanelState: useSingleAgentPanelState,
    setActiveTab: 'setSingleAgentPanelActiveTab',
    activeTab: 'singleAgentPanelActiveTab',
    setExpanded: 'setSingleAgentPanelExpanded',
    storageKey: 'jiuwenclaw_single_agent_panel_state',
  },
  {
    name: 'team',
    usePanelState: useTeamPanelState,
    setActiveTab: 'setTeamAreaActiveTab',
    activeTab: 'teamAreaActiveTab',
    setExpanded: 'setTeamAreaExpanded',
    storageKey: 'jiuwenclaw_team_panel_state',
  },
]) {
  for (const initialTab of ['planning', 'review']) {
    test(`${mode.name}: opening review leaves the plugin even when the native tab is already review (${initialTab})`, async () => {
      const restoreDom = installPanelDom();
      window.localStorage.setItem(mode.storageKey, JSON.stringify({ activeTab: initialTab }));
      const root = createRoot(document.getElementById('root'));
      const store = activatePluginPanel();
      const before = store.getState();
      let panelState;
      function Probe() {
        panelState = mode.usePanelState();
        return null;
      }
      try {
        await act(async () => root.render(createElement(Probe)));
        await act(async () => panelState[mode.setActiveTab]('review'));
        assert.equal(panelState[mode.activeTab], 'review');
        assert.equal(store.getState().activeId, null);
        assert.deepEqual(store.getState().tabs, before.tabs, 'plugin and draft tabs remain available');
        assert.equal(store.getState().draftSeq, before.draftSeq);
      } finally {
        await act(async () => root.unmount());
        store.reset();
        restoreDom();
      }
    });
  }

  test(`${mode.name}: collapsing and expanding preserves plugin selection`, async () => {
    const restoreDom = installPanelDom();
    const root = createRoot(document.getElementById('root'));
    const store = activatePluginPanel();
    let panelState;
    function Probe() {
      panelState = mode.usePanelState();
      return null;
    }
    try {
      await act(async () => root.render(createElement(Probe)));
      await act(async () => panelState[mode.setExpanded](false));
      await act(async () => panelState[mode.setExpanded](true));
      assert.equal(store.getState().activeId, PLUGIN_A);
    } finally {
      await act(async () => root.unmount());
      store.reset();
      restoreDom();
    }
  });
}

for (const request of [
  { name: 'single agent browser', open: () => openSingleAgentPanel('browser') },
  { name: 'team browser', open: () => openTeamPanel('browser') },
  { name: 'artifact', open: () => openArtifactPanel('artifact-1') },
]) {
  test(`opening ${request.name} without a mounted host clears plugin selection`, () => {
    const restoreDom = installPanelDom();
    const store = activatePluginPanel();
    const before = store.getState();
    try {
      request.open();
      assert.equal(store.getState().activeId, null);
      assert.deepEqual(store.getState().tabs, before.tabs);
    } finally {
      store.reset();
      restoreDom();
    }
  });
}
