import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';

// Reserved, non-resolving DOM origin only; fetch and WebSocket below reject all network access.
const dom = new JSDOM('<div id="root"></div>', { url: 'https://input-area.invalid', pretendToBeVisual: true });
const globals = {
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  localStorage: dom.window.localStorage,
  Node: dom.window.Node,
  HTMLElement: dom.window.HTMLElement,
  MutationObserver: dom.window.MutationObserver,
  CustomEvent: dom.window.CustomEvent,
  getComputedStyle: dom.window.getComputedStyle.bind(dom.window),
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
  ResizeObserver: class {
    observe() {}
    unobserve() {}
    disconnect() {}
  },
  fetch: () => {
    throw new Error('InputArea permission interactions must not perform HTTP requests');
  },
  WebSocket: class {
    constructor() {
      throw new Error('Unexpected WebSocket connection');
    }
  },
};
const descriptors = new Map(Object.keys(globals).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
for (const [key, value] of Object.entries(globals)) {
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
}
after(() => {
  dom.window.close();
  for (const [key, descriptor] of descriptors) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else delete globalThis[key];
  }
});

const { InputArea } =
  await import('../node_modules/.cache/input-area-permission-merge/components/ChatPanel/InputArea.js');
const { useChatStore, useSessionStore, useWorkspaceStore } =
  await import('../node_modules/.cache/input-area-permission-merge/stores/index.js');
const { default: i18n } = await import('../node_modules/.cache/input-area-permission-merge/i18n/index.js');

function byId(id, variant) {
  const selector = `[data-testid="${id}"]${variant === undefined ? '' : `[data-variant="${variant}"]`}`;
  const matches = document.querySelectorAll(selector);
  assert.equal(matches.length, 1, `${selector} must identify one actual InputArea element`);
  return matches[0];
}
const click = async (element) => act(async () => element.click());

async function mount({ mode = 'agent', profile = 'default', language = 'en' } = {}, run) {
  const sessionId = 'input-permission-merge';
  useSessionStore.getState().ensureRuntime(sessionId);
  useSessionStore.getState().setMode(sessionId, mode);
  useChatStore.getState().setActiveSessionId(sessionId);
  const previousWorkspace = useWorkspaceStore.getState();
  useWorkspaceStore.setState({ workMode: 'work', projects: [], selectedProject: null });
  await i18n.changeLanguage(language);
  const saved = [];
  const switched = [];
  const props = {
    onSubmit() {},
    onInterrupt() {},
    onCancel() {},
    onPersistMedia: async () => ({}),
    onPersistDocuments: async () => ({}),
    onSwitchMode: (next) => {
      switched.push(next);
      useSessionStore.getState().setMode(sessionId, next);
    },
    isProcessing: false,
    permissionProfile: profile,
    onSavePermission: async (update) => {
      saved.push(update);
    },
  };
  const root = createRoot(document.getElementById('root'));
  const render = async () =>
    act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(InputArea, props))));
  try {
    await render();
    await run({ saved, switched, props, render, sessionId });
  } finally {
    await act(async () => root.unmount());
    useChatStore.getState().setActiveSessionId(null);
    useChatStore.getState().removeRuntime(sessionId);
    useSessionStore.getState().removeRuntime(sessionId);
    useWorkspaceStore.setState(previousWorkspace, true);
  }
}

for (const language of ['zh', 'en']) {
  test(`${language}: mode tooltip uses option DOMRect and already translated text`, async () => {
    await mount({ language }, async ({ switched }) => {
      await click(byId('chat-panel-mode-select-trigger'));
      const option = byId('chat-panel-mode-select-option', 'team');
      option.getBoundingClientRect = () => new dom.window.DOMRect(210, 320, 140, 46);
      await act(async () => option.dispatchEvent(new dom.window.MouseEvent('mouseover', { bubbles: true })));
      const tooltip = byId('chat-panel-mode-select-tooltip');
      assert.equal(tooltip.classList.contains('adaptive-tooltip'), true);
      assert.equal(tooltip.textContent, i18n.t('chat.config.mode.clusterDesc'));
      assert.notEqual(tooltip.textContent, 'chat.config.mode.clusterDesc');
      assert.equal(tooltip.style.position, 'fixed');
      assert.equal(tooltip.style.top, '326px');
      assert.equal(tooltip.style.left, '361px');
      await click(option);
      assert.deepEqual(switched, ['team']);
      assert.equal(document.querySelector('[data-testid="chat-panel-mode-select-tooltip"]'), null);
    });
  });
}

for (const mode of ['agent', 'auto_harness']) {
  test(`${mode}: persisted automatic profile is projected without saving during render`, async () => {
    await mount({ mode, profile: 'automatic' }, async ({ saved, sessionId }) => {
      const effective = mode === 'agent' ? 'automatic' : 'default';
      const trigger = byId('chat-panel-permission-selector-trigger');
      assert.equal(trigger.dataset.variant, effective);
      assert.match(trigger.textContent, new RegExp(i18n.t(`chat.config.permission.${effective}`)));
      await click(trigger);
      const options = [...document.querySelectorAll('[data-testid="chat-panel-permission-selector-option"]')];
      assert.deepEqual(
        options.map((option) => option.dataset.variant),
        mode === 'agent' ? ['default', 'automatic', 'full_access'] : ['default', 'full_access'],
      );
      assert.equal(byId('chat-panel-permission-selector-option', effective).getAttribute('aria-checked'), 'true');
      await click(byId('chat-panel-permission-selector-option', effective));
      assert.deepEqual(saved, []);
      await act(async () => useSessionStore.getState().setMode(sessionId, 'agent'));
      assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'automatic');
      assert.deepEqual(saved, [], 'a mode projection must not overwrite the persisted automatic profile');
    });
  });
}

test('team hides the permission selector without overwriting the persisted profile', async () => {
  await mount({ mode: 'team', profile: 'automatic' }, async ({ saved, sessionId }) => {
    assert.equal(document.querySelector('[data-testid="chat-panel-permission-selector-trigger"]'), null);
    await act(async () => useSessionStore.getState().setMode(sessionId, 'agent'));
    assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'automatic');
    assert.deepEqual(saved, []);
  });
});

test('automatic selection sends the profile contract and reflects the persisted prop', async () => {
  await mount({}, async ({ saved, props, render }) => {
    await click(byId('chat-panel-permission-selector-trigger'));
    await click(byId('chat-panel-permission-selector-option', 'automatic'));
    assert.deepEqual(saved, [{ permissions_profile: 'automatic' }]);
    assert.equal(document.querySelector('[data-testid="chat-panel-perm-warning-modal"]'), null);
    props.permissionProfile = 'automatic';
    await render();
    assert.equal(byId('chat-panel-permission-selector-trigger').dataset.variant, 'automatic');
  });
});

for (const action of ['cancel', 'confirm']) {
  test(`full access ${action} preserves the warning flow`, async () => {
    await mount({}, async ({ saved }) => {
      await click(byId('chat-panel-permission-selector-trigger'));
      await click(byId('chat-panel-permission-selector-option', 'full_access'));
      assert.deepEqual(saved, []);
      assert.equal(
        byId('chat-panel-perm-warning-title').textContent,
        i18n.t('chat.config.permission.fullAccessWarning.title'),
      );
      await click(byId(`chat-panel-perm-warning-${action}`));
      assert.deepEqual(saved, action === 'confirm' ? [{ permissions_profile: 'full_access' }] : []);
      assert.equal(document.querySelector('[data-testid="chat-panel-perm-warning-modal"]'), null);
    });
  });
}
