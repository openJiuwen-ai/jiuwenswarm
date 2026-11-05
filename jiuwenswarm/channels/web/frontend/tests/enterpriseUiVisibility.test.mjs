import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM, VirtualConsole } from 'jsdom';
import { createServer } from 'vite';
import svgr from 'vite-plugin-svgr';
import { A2UIProvider } from '@a2ui/react';

const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => {
  // JSDOM cannot parse the optional A2UI library's modern CSS; no styles are asserted here.
  if (error.type !== 'css parsing') throw error;
});
const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/', pretendToBeVisual: true, virtualConsole });
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
after(() => {
  dom.window.close();
  for (const [name, descriptor] of previousGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});
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
  cacheDir: 'node_modules/.cache/enterprise-ui-visibility/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
  define: { 'import.meta.env.DEV': false },
});
let modules;
try {
  modules = await Promise.all([
    vite.ssrLoadModule('/src/multi-session/sidebar/ConversationSidebar.tsx'),
    vite.ssrLoadModule('/src/stores/workspaceStore.ts'),
    vite.ssrLoadModule('/src/components/ChatPanel/index.tsx'),
    vite.ssrLoadModule('/src/components/ToolPanel/index.tsx'),
    vite.ssrLoadModule('/src/stores/chatStore.ts'),
    vite.ssrLoadModule('/src/stores/sessionStore.ts'),
    vite.ssrLoadModule('/src/stores/authStore.ts'),
    vite.ssrLoadModule('/src/features/historyRestore.ts'),
    vite.ssrLoadModule('/src/services/webClient.ts'),
  ]);
} finally {
  await vite.close();
}
const [
  { ConversationSidebar }, { useWorkspaceStore }, { ChatPanel }, { ToolPanel },
  { useChatStore }, { useSessionStore }, { useAuthStore },
  { parseHistoryJsonFileToTimelinePreview }, { webClient },
] = modules;
const i18n = i18next.createInstance();
await i18n.init({
  lng: 'zh',
  initImmediate: false,
  showSupportNotice: false,
  resources: { zh: { translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8')) } },
});

async function withSidebar(edition, workMode, run) {
  const previousWorkspace = useWorkspaceStore.getState();
  window.__JIUWENSWARM_EDITION__ = edition;
  useWorkspaceStore.setState({ workMode, projects: [], pinnedSessions: [], loadProjects: async () => true });
  const root = createRoot(document.getElementById('root'));
  let newConversations = 0;
  const byId = id => document.querySelector(`[data-testid="${id}"]`);
  const click = element => act(async () => {
    assert.ok(element, 'click target exists');
    element.click();
  });
  try {
    await act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(ConversationSidebar, {
      activeSessionId: null,
      onNew: () => { newConversations += 1; },
      onSelect() {},
      onOpenCron() {},
      isCronActive: false,
      peek: true,
    }))));
    await run({ byId, click, newConversations: () => newConversations });
  } finally {
    await act(async () => root.unmount());
    useWorkspaceStore.setState(previousWorkspace, true);
    delete window.__JIUWENSWARM_EDITION__;
    localStorage.clear();
  }
}

for (const initialMode of ['work', 'code', 'design']) {
  test(`enterprise locks ${initialMode} to work without a dropdown or faded label`, () =>
    withSidebar('enterprise', initialMode, async ({ byId, click, newConversations }) => {
      const trigger = byId('multi-session-work-mode-trigger');
      assert.equal(byId('multi-session-work-mode-label').textContent, i18n.t('codeMode.work'));
      assert.equal(useWorkspaceStore.getState().workMode, 'work');
      assert.equal(trigger.disabled, false);
      assert.equal(trigger.style.cursor, 'default');
      assert.equal(trigger.querySelector('svg'), null);
      assert.equal(trigger.hasAttribute('aria-haspopup'), false);
      await click(trigger);
      assert.equal(byId('multi-session-work-mode-menu'), null);
      assert.equal(newConversations(), 0);
    }));
}

test('personal keeps its saved mode and can switch among work, code and design', () =>
  withSidebar('personal', 'code', async ({ byId, click, newConversations }) => {
    const trigger = byId('multi-session-work-mode-trigger');
    assert.equal(useWorkspaceStore.getState().workMode, 'code');
    assert.equal(byId('multi-session-work-mode-label').dataset.variant, 'code');
    assert.ok(trigger.querySelector('svg'));
    for (const mode of ['design', 'work', 'code']) {
      await click(trigger);
      await click(byId(`multi-session-work-mode-menu-${mode}`));
      assert.equal(useWorkspaceStore.getState().workMode, mode);
      assert.equal(byId('multi-session-work-mode-label').dataset.variant, mode);
      assert.equal(byId('multi-session-work-mode-menu'), null);
    }
    assert.equal(newConversations(), 3);
  }));

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
async function withChatTools(edition, run) {
  const stores = [useChatStore, useSessionStore, useAuthStore];
  const previous = stores.map(store => store.getState());
  const originalRequest = webClient.request;
  webClient.request = async () => ({ skills: [], commands: [], groups: [], agentGroups: [], items: [] });
  window.__JIUWENSWARM_EDITION__ = edition;
  useAuthStore.setState({ initialized: true, enabled: false, islogin: true, campaignState: 'off' });
  for (const sid of ['ui-session-a', 'ui-session-b']) {
    useChatStore.getState().ensureRuntime(sid);
    useSessionStore.getState().ensureRuntime(sid);
  }
  useChatStore.getState().setActiveSessionId('ui-session-a');
  const root = createRoot(document.getElementById('root'));
  const byId = id => document.querySelector(`[data-testid="${id}"]`);
  const render = component => act(async () => root.render(createElement(I18nextProvider, { i18n }, createElement(A2UIProvider, { onAction: noop }, component))));
  try {
    await run({ render, byId });
  } finally {
    await act(async () => root.unmount());
    stores.forEach((store, index) => store.setState(previous[index], true));
    webClient.request = originalRequest;
    delete window.__JIUWENSWARM_EDITION__;
  }
}

for (const edition of ['enterprise', 'personal']) {
  test(`${edition} only exposes export and heartbeat in personal edition`, () => withChatTools(edition, async ({ render, byId }) => {
    let exports = 0;
    let heartbeats = 0;
    useChatStore.getState().addMessage('ui-session-a', { id: 'message-a', role: 'assistant', content: 'test', timestamp: '2026-10-23T00:00:00Z' });
    await render(createElement(ChatPanel, {
      promptSuggestionBox,
      onSendMessage: noop, onEnsureSession: async () => 'ui-session-a', onForkSession: noop,
      onPersistMedia: async () => ({}), onPersistDocuments: async () => ({}),
      onInterrupt: noop, onCancel: noop, onSwitchMode: noop, onUserAnswer: async () => true,
      isProcessing: false, permissionProfile: 'default', onSavePermission: async () => {},
      canExportShare: true, onExportShare: () => { exports += 1; },
      onToggleHeartbeatPanel: () => { heartbeats += 1; },
    }));
    if (edition === 'enterprise') {
      assert.equal(byId('chat-panel-share-export'), null);
      assert.equal(byId('chat-panel-heartbeat-trigger'), null);
      assert.equal(exports, 0);
    } else {
      await act(async () => byId('chat-panel-share-export').click());
      assert.equal(exports, 1);
      await act(async () => byId('chat-panel-heartbeat-trigger').click());
      assert.equal(heartbeats, 1);
    }
  }));
}

test('enterprise references merge selected and called skills, restore history and follow the active session', () =>
  withChatTools('enterprise', async ({ render, byId }) => {
    const sid = 'ui-session-a';
    const timestamp = '2026-10-23T00:00:00Z';
    const records = [
      { role: 'user', content: 'selected skill', skills: ['selected-skill'], timestamp },
      { role: 'assistant', event_type: 'chat.tool_call', timestamp, tool_call: { tool_call_id: 'skill-call-a', name: 'skill_tool', arguments: JSON.stringify({ skill_name: 'called-skill' }) } },
    ];
    const history = parseHistoryJsonFileToTimelinePreview(records, sid);
    assert.equal(history.executions.length, 1);
    const restore = () => useChatStore.setState(state => ({ runtimes: {
      ...state.runtimes,
      [sid]: { ...state.runtimes[sid], messages: history.messages, toolExecutions: new Map(history.executions.map(item => [item.toolCallId, item])) },
    } }));
    restore();
    useChatStore.getState().addMessage(sid, { id: 'duplicate', role: 'user', content: 'again', skills: ['selected-skill'], timestamp });
    const panel = () => createElement(ToolPanel, {
      sessionId: useChatStore.getState().activeSessionId,
      teamAreaExpanded: false, teamAreaActiveTab: 'planning', teamAreaActiveDetailTab: 'messages',
      singleAgentPanelExpanded: false, singleAgentPanelActiveTab: 'planning',
      setTeamAreaExpanded: noop, setTeamAreaActiveTab: noop, setTeamAreaActiveDetailTab: noop,
      setTeamAreaSelectedMemberId: noop, setTeamAreaSelectedArtifactId: noop,
      setSingleAgentPanelExpanded: noop, setSingleAgentPanelActiveTab: noop,
      setSingleAgentPanelSelectedArtifactId: noop, setSingleAgentPanelSelectedSubagentId: noop,
    });
    await render(panel());
    const referenceNames = () => Array.from(byId('tool-panel-references-pane').querySelectorAll('[data-testid="team-area-task-planning-task-title"]'), el => el.textContent);
    assert.deepEqual(referenceNames(), ['selected-skill', 'called-skill']);
    await act(async () => useChatStore.getState().setActiveSessionId('ui-session-b'));
    await render(panel());
    assert.deepEqual(referenceNames(), []);
    await act(async () => { restore(); useChatStore.getState().setActiveSessionId(sid); });
    await render(panel());
    assert.deepEqual(referenceNames(), ['selected-skill', 'called-skill']);
  }));
