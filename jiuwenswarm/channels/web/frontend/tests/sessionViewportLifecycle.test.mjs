import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';
import { createServer } from 'vite';
import svgr from 'vite-plugin-svgr';
import { act, createElement as h } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM, VirtualConsole } from 'jsdom';
import { A2UIProvider } from '@a2ui/react';

const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => {
  if (error.type !== 'css parsing') throw error;
});
const dom = new JSDOM('<!doctype html><div id="root"></div>', {
  url: 'http://localhost/',
  pretendToBeVisual: true,
  virtualConsole,
});
dom.window.matchMedia = query => ({ matches: false, media: query, addEventListener() {}, removeEventListener() {} });
const previousGlobals = new Map();
for (const [name, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  navigator: dom.window.navigator,
  Node: dom.window.Node,
  HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element,
  CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver,
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  requestAnimationFrame: dom.window.requestAnimationFrame.bind(dom.window),
  cancelAnimationFrame: dom.window.cancelAnimationFrame.bind(dom.window),
  IS_REACT_ACT_ENVIRONMENT: true,
})) {
  previousGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
}

const geometry = { height: 3003, rowHeight: 120, suppressScrollEvents: false };
let offsets = new WeakMap();
const descriptors = new Map();
for (const [name, descriptor] of [
  ['clientHeight', { get() { return this.hasAttribute('data-timeline-scroll-root') ? 467 : 0; } }],
  ['offsetHeight', {
    get() {
      if (this.hasAttribute('data-timeline-scroll-root')) return 467;
      return this.dataset.index === undefined ? 0 : geometry.rowHeight;
    },
  }],
  ['scrollHeight', {
    get() {
      if (!this.hasAttribute('data-timeline-scroll-root')) return 0;
      return this.querySelector('[data-testid="chat-panel-welcome"]') ? 400 : geometry.height;
    },
  }],
  ['scrollTop', {
    get() {
      if (!this.hasAttribute('data-timeline-scroll-root')) return 0;
      return Math.max(0, Math.min(offsets.get(this) ?? 0, this.scrollHeight - this.clientHeight));
    },
    set(value) { offsets.set(this, value); },
  }],
]) {
  const prototype = name === 'clientHeight' || name === 'offsetHeight' || name === 'scrollHeight' || name === 'scrollTop'
    ? dom.window.HTMLElement.prototype
    : dom.window.Element.prototype;
  descriptors.set(name, Object.getOwnPropertyDescriptor(prototype, name));
  Object.defineProperty(prototype, name, { configurable: true, ...descriptor });
}
const rectDescriptor = Object.getOwnPropertyDescriptor(dom.window.Element.prototype, 'getBoundingClientRect');
Object.defineProperty(dom.window.Element.prototype, 'getBoundingClientRect', {
  configurable: true,
  writable: true,
  value() {
    if (this.hasAttribute('data-timeline-scroll-root')) return { top: 0, width: 800, height: 467 };
    return {
      top: (Number.parseFloat(this.style.top) || 0) - (this.closest('[data-timeline-scroll-root]')?.scrollTop ?? 0),
      width: 800,
      height: this.offsetHeight,
    };
  },
});
dom.window.HTMLElement.prototype.scrollTo = function scrollTo({ top }) {
  const previous = this.scrollTop;
  this.scrollTop = top;
  if (!geometry.suppressScrollEvents && this.scrollTop !== previous) {
    this.dispatchEvent(new dom.window.Event('scroll'));
  }
};

const vite = await createServer({
  configFile: false,
  plugins: [svgr(), {
    name: 'no-optional-application-plugins',
    enforce: 'pre',
    transform(code, id) {
      if (!id.endsWith('/applicationPlugins/ApplicationPluginOutlet.tsx')) return;
      return code.replace("import.meta.glob<BundledPluginModule>('../../../../../extensions/*/frontend/index.tsx', { eager: true })", '{}');
    },
  }],
  resolve: { dedupe: ['react', 'react-dom', 'react-i18next', 'i18next', 'zustand'] },
  cacheDir: 'node_modules/.cache/session-viewport-lifecycle/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
  define: { 'import.meta.env.DEV': false },
});
let modules;
try {
  modules = await Promise.all([
    vite.ssrLoadModule('/src/components/ChatPanel/index.tsx'),
    vite.ssrLoadModule('/src/stores/index.ts'),
    vite.ssrLoadModule('/src/stores/authStore.ts'),
    vite.ssrLoadModule('/src/services/webClient.ts'),
    vite.ssrLoadModule('/src/features/chatTimeline/sessionViewportCache.ts'),
  ]);
} finally {
  await vite.close();
}
const [
  { ChatPanel },
  { ensureSessionRuntimes, useChatStore, useSessionStore },
  { useAuthStore },
  { webClient },
  {
    acquireSessionViewportToken,
    deleteSessionViewport,
    getSessionScrollRestore,
    getSessionTimelineViewport,
    releaseSessionViewportToken,
    saveSessionScrollTop,
  },
] = modules;
const i18n = i18next.createInstance();
await i18n.init({
  lng: 'zh',
  initImmediate: false,
  showSupportNotice: false,
  resources: { zh: { translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8')) } },
});
const originalRequest = webClient.request;
webClient.request = async () => ({ skills: [], commands: [], groups: [], agentGroups: [], items: [] });
const noop = () => {};
const promptSuggestionBox = {
  text: null,
  suggestions: [],
  accept: noop,
  acceptForSend: noop,
  dismiss: noop,
  takeSubmission: () => ({ settle: noop }),
  setEnabled: noop,
  markShown: noop,
};
const settle = () => new Promise(resolve => setTimeout(resolve, 0));

after(() => {
  webClient.request = originalRequest;
  for (const [name, descriptor] of descriptors) {
    const prototype = name === 'clientHeight' || name === 'offsetHeight' || name === 'scrollHeight' || name === 'scrollTop'
      ? dom.window.HTMLElement.prototype
      : dom.window.Element.prototype;
    if (descriptor) Object.defineProperty(prototype, name, descriptor);
    else delete prototype[name];
  }
  if (rectDescriptor) Object.defineProperty(dom.window.Element.prototype, 'getBoundingClientRect', rectDescriptor);
  delete dom.window.HTMLElement.prototype.scrollTo;
  dom.window.close();
  for (const [name, descriptor] of previousGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

async function mountConversation(sessionId, { height = 3003, messageCount = 40, rowHeight = 120 } = {}) {
  geometry.height = height;
  geometry.rowHeight = rowHeight;
  geometry.suppressScrollEvents = false;
  offsets = new WeakMap();
  ensureSessionRuntimes(sessionId);
  ensureSessionRuntimes('new');
  useAuthStore.setState({ initialized: true, enabled: false, islogin: false, campaignState: 'off' });
  useSessionStore.setState({ availableModels: [], chatAvailableModels: [], defaultModelName: null });
  for (let index = 0; index < messageCount; index += 1) {
    useChatStore.getState().addMessage(sessionId, {
      id: `${sessionId}-message-${index}`,
      role: index % 2 === 0 ? 'user' : 'assistant',
      content: `Message ${index}`,
      timestamp: '2026-10-09T08:00:00Z',
    });
  }
  useChatStore.getState().setProcessing(sessionId, false);
  useChatStore.getState().setActiveSessionId(sessionId);
  let root = createRoot(document.getElementById('root'));
  const render = () => h(I18nextProvider, { i18n }, h(A2UIProvider, { onAction: noop }, h(ChatPanel, {
    promptSuggestionBox,
    onSendMessage: noop,
    isProcessing: false,
  })));
  await act(async () => root.render(render()));
  await act(settle);
  let scrollRoot = document.querySelector('[data-testid="chat-panel-scroll"]');
  assert.ok(scrollRoot);
  return {
    scrollRoot,
    metrics() {
      return {
        top: scrollRoot.scrollTop,
        height: scrollRoot.scrollHeight,
        bottomGap: scrollRoot.scrollHeight - scrollRoot.scrollTop - scrollRoot.clientHeight,
        keys: Array.from(scrollRoot.querySelectorAll('[data-testid="chat-panel-timeline-row"]'), row => row.dataset.variant),
      };
    },
    async navigate(nextSessionId) {
      await act(async () => {
        useChatStore.getState().setActiveSessionId(nextSessionId);
        root.render(render());
        await settle();
      });
    },
    async coldRestart(nextSessionId, beforeRemount) {
      await act(async () => root.unmount());
      useChatStore.getState().setActiveSessionId(nextSessionId);
      beforeRemount();
      root = createRoot(document.getElementById('root'));
      await act(async () => root.render(render()));
      await act(settle);
      scrollRoot = document.querySelector('[data-testid="chat-panel-scroll"]');
      assert.ok(scrollRoot);
    },
    async addMessage(id) {
      await act(async () => {
        useChatStore.getState().addMessage(sessionId, {
          id,
          role: 'assistant',
          content: 'Arrived before the next scroll event',
          timestamp: '2026-10-09T08:01:00Z',
        });
        await settle();
      });
    },
    async scroll(top, dispatch = true) {
      await act(async () => {
        scrollRoot.scrollTop = top;
        if (dispatch) scrollRoot.dispatchEvent(new dom.window.Event('scroll'));
        await settle();
      });
    },
    setHeight(value) { geometry.height = value; },
    suppressScrollEvents() { geometry.suppressScrollEvents = true; },
    async unmount() {
      await act(async () => root.unmount());
      deleteSessionViewport(sessionId);
      deleteSessionViewport('new');
      document.getElementById('root').replaceChildren();
    },
  };
}

test('a non-bottom session survives new-task navigation and a message before the next scroll event', async () => {
  const sessionId = 'viewport-behavior-non-bottom';
  const panel = await mountConversation(sessionId);
  try {
    assert.equal(panel.metrics().bottomGap, 0);
    await panel.scroll(1336);
    await panel.navigate('new');
    panel.suppressScrollEvents();
    await panel.navigate(sessionId);
    const restored = panel.metrics();
    assert.equal(restored.top, 1336);
    assert.equal(restored.bottomGap, 1200);
    assert.ok(restored.keys.length > 0);

    panel.setHeight(4003);
    await panel.addMessage(`${sessionId}-after-return`);
    assert.deepEqual(panel.metrics(), { ...restored, height: 4003, bottomGap: 2200 });
  } finally {
    await panel.unmount();
  }
});

test('a bottom session keeps following growth after new-task navigation without a scroll event', async () => {
  const sessionId = 'viewport-behavior-bottom';
  const panel = await mountConversation(sessionId);
  try {
    assert.equal(panel.metrics().bottomGap, 0);
    await panel.navigate('new');
    panel.suppressScrollEvents();
    await panel.navigate(sessionId);
    assert.equal(panel.metrics().bottomGap, 0);

    panel.setHeight(4003);
    await panel.addMessage(`${sessionId}-after-return`);
    assert.equal(panel.metrics().top, 3536);
    assert.equal(panel.metrics().bottomGap, 0);
  } finally {
    await panel.unmount();
  }
});

test('a cold mount restores outer scroll without a timeline anchor', async () => {
  const sessionId = 'viewport-behavior-cold-outer-scroll';
  const panel = await mountConversation(sessionId);
  try {
    await panel.scroll(1336);
    await panel.coldRestart(sessionId, () => {
      // Isolate ChatPanel's cold-mount restoration from VirtualTimeline's anchor recovery.
      deleteSessionViewport(sessionId);
      const token = acquireSessionViewportToken(sessionId);
      saveSessionScrollTop(token, 1336, false);
      releaseSessionViewportToken(token);
      panel.suppressScrollEvents();
    });
    assert.equal(panel.metrics().top, 1336);
    assert.equal(panel.metrics().bottomGap, 1200);
  } finally {
    await panel.unmount();
  }
});

test('a cold mount restores the saved timeline anchor after history prepends', async () => {
  const sessionId = 'viewport-behavior-cold-anchor';
  // A non-estimated row height forces the virtualizer to persist real measurements.
  const panel = await mountConversation(sessionId, { rowHeight: 121 });
  try {
    await panel.scroll(1336);
    const before = panel.metrics();
    assert.ok(before.keys.length > 0);
    const prependCount = 4;
    await panel.coldRestart(sessionId, () => {
      const saved = getSessionTimelineViewport(sessionId);
      assert.ok(saved);
      assert.equal(saved.offset, before.top);
      assert.ok(saved.measurements.length > 0);
      assert.ok(saved.anchorKey);
      useChatStore.getState().prependMessages(sessionId, Array.from({ length: prependCount }, (_, index) => ({
        id: `${sessionId}-older-${index}`,
        role: index % 2 === 0 ? 'user' : 'assistant',
        content: `Older message ${index}`,
        timestamp: '2026-10-09T07:00:00Z',
      })));
    });
    const restored = panel.metrics();
    assert.ok(restored.keys.length > 0);
    assert.ok(restored.top > before.top);
    assert.deepEqual(restored.keys, before.keys);
  } finally {
    await panel.unmount();
  }
});

test('immediate DOM scrolling survives an immediate new-task switch before its scroll event', async () => {
  const sessionId = 'viewport-behavior-immediate-scroll';
  const panel = await mountConversation(sessionId, { height: 5003 });
  try {
    await panel.scroll(3200, false);
    await panel.navigate('new');
    panel.suppressScrollEvents();
    await panel.navigate(sessionId);
    assert.equal(panel.metrics().top, 3200);
    assert.equal(panel.metrics().bottomGap, 1336);
  } finally {
    await panel.unmount();
  }
});

test('permanent deletion rejects delayed ChatPanel and VirtualTimeline unmount writes', async () => {
  const sessionId = 'viewport-behavior-deleted';
  const panel = await mountConversation(sessionId);
  try {
    await panel.scroll(1336);
    deleteSessionViewport(sessionId);
    for (let index = 0; index < 20; index += 1) deleteSessionViewport(`viewport-behavior-deleted-${index}`);
    await panel.navigate('new');

    assert.equal(getSessionScrollRestore(sessionId, 3003, 467), undefined);
    assert.equal(getSessionTimelineViewport(sessionId), undefined);
  } finally {
    await panel.unmount();
  }
});

// These entry-point checks intentionally remain source contracts: they verify call coverage,
// while the mounted tests above own the runtime lifecycle assertions.
const readSource = path => readFileSync(new URL(`../${path}`, import.meta.url), 'utf8');

test('both session deletion paths clear the shared viewport cache', () => {
  const app = readSource('src/App.tsx');
  const sidebar = readSource('src/multi-session/sidebar/useSidebarMenu.tsx');
  assert.match(app, /deleteSessionViewport\(id\)/);
  assert.match(sidebar, /deleteSessionViewport\(sessionId\)/);
});

test('all permanent deletion entry points invalidate viewports while archive paths retain them', () => {
  const archived = readSource('src/features/settings/modules/archivedTasks/ArchivedTasksSettings.tsx');
  const sessions = readSource('src/components/SessionsPanel/index.tsx');
  const websocket = readSource('src/hooks/useWebSocket.ts');
  const sidebar = readSource('src/multi-session/sidebar/useSidebarMenu.tsx');

  assert.equal((archived.match(/deleteSessionViewport\(session\.session_id\)/g) || []).length, 2);
  assert.match(archived, /filter\(\(item\) => item\.ok\)[\s\S]*deleteSessionViewport\(item\.session_id\)/);
  assert.match(sessions, /await webRequest\('session\.delete'[\s\S]*deleteSessionViewport\(sessionId\)/);
  assert.match(websocket, /'session\.deleted'[\s\S]*deleteSessionViewport\(sessionId\)/);

  const archiveAction = sidebar.slice(sidebar.indexOf('async function archiveSessionAction'), sidebar.indexOf('async function deleteSession'));
  assert.doesNotMatch(archiveAction, /deleteSessionViewport/);
  const archiveEvents = websocket.slice(websocket.indexOf("'session.archived'"), websocket.indexOf("'session.deleted'"));
  assert.doesNotMatch(archiveEvents, /deleteSessionViewport/);
});
