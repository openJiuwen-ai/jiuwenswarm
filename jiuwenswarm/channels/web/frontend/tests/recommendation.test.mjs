import assert from 'node:assert/strict';
import test, { before, after, describe } from 'node:test';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';

import {
  buildTopicStarterRequest,
  buildDynamicTopicStarterSlate,
  buildDirectInputEvent,
  buildExposureEvent,
  buildNotInterestedEvent,
  buildPageLeftEvent,
  buildSessionBoundEvent,
  buildTopicClickEvent,
  topicStarterShowsProviderRefusal,
  topicStarterShowsRetryAction,
  topicStarterShowsSettingsAction,
  topicStarterShowsStatus,
  topicStarterStatusMessageKey,
} from '../node_modules/.cache/topic-starter-visibility/features/topicStarterVisibility.js';

// Topic starter request/event builders.
function topic(index, batchId = 'batch-a') {
  return {
    id: `revision-${index}`,
    card_revision_id: `revision-${index}`,
    generation_batch_id: batchId,
    position: index - 1,
    title: `Topic ${index}`,
    prompt: `Discuss topic ${index}`,
    evidence: [{ url: `https://example.com/${index}` }],
  };
}

function response(overrides = {}) {
  return {
    available: true,
    session_id: 'session-a',
    slate_id: 'slate-a',
    manifest_id: 'manifest-a',
    generation_batch_id: 'batch-a',
    profile_version: 3,
    policy_id: 'topic-starter-mvp-v6',
    context_scope: 'global_new_chat',
    project_id: null,
    short_intent_version: 2,
    pool_card_count: 7,
    exposure_slice_id: 'slice-a',
    selection_mode: 'mixed_dynamic',
    topics: [topic(1), topic(2)],
    ...overrides,
  };
}

const dynamicSlate = {
  kind: 'dynamic',
  sessionId: 'session-a',
  slateId: 'slate-a',
  manifestId: 'manifest-a',
  generationBatchId: 'batch-a',
  profileVersion: 3,
  policyId: 'topic-starter-mvp-v6',
  contextScope: 'global_new_chat',
  projectId: null,
  shortIntentVersion: 2,
  exposureSliceId: 'slice-a',
  selectionMode: 'mixed_dynamic',
  topics: [topic(1), topic(2)],
};

test('collapses homepage recommendation status into three jobs', () => {
  assert.equal(topicStarterShowsStatus('ready', false), false);
  assert.equal(topicStarterShowsStatus('preparing', false), true);
  assert.equal(topicStarterStatusMessageKey('preparing', false), 'chat.topicStarter.statusPreparing');
  assert.equal(topicStarterStatusMessageKey('preparing', true), 'chat.topicStarter.statusPreparingWithCards');
  assert.equal(topicStarterShowsSettingsAction('preparing'), false);
  assert.equal(topicStarterShowsStatus('disabled', true), true);
  assert.equal(topicStarterStatusMessageKey('disabled', true), 'chat.topicStarter.statusNeedsSettings');
  assert.equal(topicStarterStatusMessageKey('disabled', false), 'chat.topicStarter.statusNeedsSettings');
  assert.equal(topicStarterStatusMessageKey('missing_configuration', false), 'chat.topicStarter.statusNeedsSettings');
  assert.equal(topicStarterStatusMessageKey('provider_unavailable', false), 'chat.topicStarter.statusNeedsSettings');
  assert.equal(topicStarterStatusMessageKey('waiting_for_input', false), 'chat.topicStarter.statusNoCards');
  assert.equal(topicStarterStatusMessageKey('no_content', false), 'chat.topicStarter.statusNoCards');
  assert.equal(topicStarterStatusMessageKey('failed', false), 'chat.topicStarter.statusNoCards');
  assert.equal(topicStarterStatusMessageKey('failed', true), 'chat.topicStarter.statusUpdateIncomplete');
  assert.equal(topicStarterShowsSettingsAction('disabled'), true);
  assert.equal(topicStarterShowsSettingsAction('failed'), false);
  assert.equal(topicStarterShowsRetryAction('waiting_for_input'), true);
  assert.equal(topicStarterShowsRetryAction('disabled'), false);
  assert.equal(topicStarterShowsProviderRefusal('provider_unavailable'), true);
  assert.equal(topicStarterShowsProviderRefusal('failed'), false);
});

test('builds isolated global and project new-chat requests', () => {
  assert.deepEqual(buildTopicStarterRequest('session-a', null), {
    session_id: 'session-a',
    policy_id: 'topic-starter-mvp-v6',
    context_scope: 'global_new_chat',
    project_id: null,
  });
  assert.deepEqual(buildTopicStarterRequest('session-b', 'project-7'), {
    session_id: 'session-b',
    policy_id: 'topic-starter-mvp-v6',
    context_scope: 'project:project-7',
    project_id: 'project-7',
  });
});

test('rejects stale or malformed pool responses', () => {
  const valid = response();
  const invalid = [
    { ...valid, session_id: 'another-session' },
    { ...valid, policy_id: 'topic-starter-mvp-v5' },
    { ...valid, profile_version: undefined },
    { ...valid, selection_mode: 'static' },
    { ...valid, topics: [] },
    { ...valid, pool_card_count: 0 },
  ];
  for (const candidate of invalid) {
    assert.equal(buildDynamicTopicStarterSlate(candidate, buildTopicStarterRequest('session-a', null)), null);
  }
});

test('click and explicit dislike carry exact ids and no client-authored card content', () => {
  const click = buildTopicClickEvent(dynamicSlate, 'revision-2');
  const dislike = buildNotInterestedEvent(dynamicSlate, 'revision-1');
  const exactFirst = {
    manifest_id: 'manifest-a',
    card_revision_id: 'revision-1',
    generation_batch_id: 'batch-a',
    context_scope: 'global_new_chat',
    project_id: null,
    short_intent_version: 2,
    exposure_slice_id: 'slice-a',
  };
  assert.equal(click.event_type, 'topic_click');
  assert.equal(click.position, 1);
  assert.deepEqual(click.payload, {
    ...exactFirst,
    card_revision_id: 'revision-2',
  });
  assert.equal(dislike.event_type, 'not_interested');
  assert.equal(dislike.position, 0);
  assert.deepEqual(dislike.payload, exactFirst);
  for (const key of ['title', 'prompt', 'intent', 'evidence']) {
    assert.equal(Object.hasOwn(click.payload, key), false);
    assert.equal(Object.hasOwn(dislike.payload, key), false);
  }
});

test('keeps the exposure position after another card is dismissed', () => {
  const filteredSlate = {
    ...dynamicSlate,
    topics: [{ ...dynamicSlate.topics[1], position: 1 }],
  };
  const click = buildTopicClickEvent(filteredSlate, 'revision-2');
  assert.equal(click.position, 1);
});

describe('recommendation settings page', () => {
  let createRoot;
  let I18nextProvider;
  let SettingsServicesProvider;
  let RecommendationSettings;
  let agentModule;
  let recommendationModule;
  let modelsModule;
  let dom;
  const previousGlobals = new Map();
  const i18n = i18next.createInstance();

  const READY_STATUS = Object.freeze({
    provider: 'ark',
    model: 'deepseek-v4-1-flash-260910',
    context_window_tokens: 131072,
    configured: true,
    missing_fields: [],
    native_search: { ddg: true, bing: true },
    native_search_missing: [],
    status: 'ready',
    last_failure: null,
    provider_refused: false,
    enabled: false,
    selected_model: 'follow_main',
    model_options: [
      { model_id: 'mdl_flash', name: 'deepseek-v4-1-flash-260910', disabled_reason: null },
      { model_id: 'mdl_pro', name: 'deepseek-v4-1-pro', disabled_reason: null },
      { model_id: 'mdl_messages', name: 'messages-model', disabled_reason: 'incompatible' },
      { model_id: 'mdl_account', name: 'account-model', disabled_reason: 'login_required' },
      { model_id: '', name: 'login-model', disabled_reason: 'free' },
    ],
  });

  const svgStubPlugin = {
    name: 'recommendation-settings-test-stubs',
    setup(builder) {
      builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-react-stub' }));
      builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
        contents: 'export default function SvgStub() { return null; }',
        loader: 'js',
      }));
      // The section talks to the gateway through webRequest; the test answers in its place.
      builder.onResolve({ filter: /\/services\/webClient$/ }, ({ path, importer }) =>
        importer.endsWith('RecommendationSettings.tsx') ? { path, namespace: 'web-client-stub' } : undefined);
      builder.onLoad({ filter: /.*/, namespace: 'web-client-stub' }, () => ({
        contents: 'export const webRequest = (method, params) => globalThis.__recommendationRequest(method, params);',
        loader: 'js',
      }));
      builder.onResolve({ filter: /SettingsSourceProvider$/ }, ({ path, importer }) =>
        importer.endsWith('RecommendationSettings.tsx') ? { path, namespace: 'settings-source-stub' } : undefined);
      builder.onLoad({ filter: /.*/, namespace: 'settings-source-stub' }, () => ({
        contents: 'export function useSettingsSource() { return { values: {}, savingKeys: new Set(), save: async () => {}, patchLocal() {} }; }',
        loader: 'js',
      }));
    },
  };

  before(async () => {
    dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true });
    for (const [name, value] of Object.entries({
      window: dom.window,
      document: dom.window.document,
      navigator: dom.window.navigator,
      HTMLElement: dom.window.HTMLElement,
      Node: dom.window.Node,
      IS_REACT_ACT_ENVIRONMENT: true,
    })) {
      previousGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
      Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
    }
    // jsdom lacks native dialog opening/closing and scrolling; the dialogs themselves still render.
    dom.window.HTMLDialogElement.prototype.showModal = function () {
      this.open = true;
    };
    dom.window.HTMLDialogElement.prototype.close = function () {
      this.open = false;
    };
    ({ createRoot } = await import('react-dom/client'));
    const result = await build({
      stdin: {
        contents: `
          export { RecommendationTopicSettings as RecommendationSettings } from './src/features/settings/modules/recommendation/RecommendationSettings';
          export { recommendationModule } from './src/features/settings/modules/recommendation';
          export { agentModule } from './src/features/settings/modules/agent';
          export { modelsModule } from './src/features/settings/modules/models';
          export { SettingsServicesProvider } from './src/features/settings/services/SettingsServicesProvider';
        `,
        resolveDir: fileURLToPath(new URL('../', import.meta.url)),
        loader: 'ts',
      },
      bundle: true,
      write: false,
      platform: 'node',
      format: 'cjs',
      packages: 'external',
      loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
      define: { 'import.meta.glob': 'testAssetGlob', 'import.meta.env.DEV': 'false' },
      banner: { js: 'const testAssetGlob = () => ({});' },
      plugins: [svgStubPlugin],
    });
    const compiled = { exports: {} };
    const require = createRequire(import.meta.url);
    new Function('require', 'module', 'exports', result.outputFiles[0].text)(require, compiled, compiled.exports);
    ({ RecommendationSettings, agentModule, recommendationModule, modelsModule, SettingsServicesProvider } = compiled.exports);
    ({ I18nextProvider } = require('react-i18next'));
    await i18n.init({
      lng: 'zh',
      showSupportNotice: false,
      resources: {
        zh: { translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8')) },
      },
    });
  });

  after(() => {
    delete globalThis.__recommendationRequest;
    dom?.window.close();
    for (const [name, descriptor] of previousGlobals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  });

  async function settle() {
    await act(async () => {
      for (let index = 0; index < 5; index += 1) await Promise.resolve();
    });
  }

  async function mount(t, {
    status = READY_STATUS, statusFails = false, profile = { units: [], permanent_directions: [] }, restoredProfile = null,
    focus = null,
  } = {}) {
    const requests = [];
    const corrections = [];
    let current = { ...status };
    globalThis.__recommendationRequest = async (method, params) => {
      requests.push({ method, params });
      if (method === 'topic_starters.status') {
        if (statusFails) throw new Error('offline');
        return current;
      }
      if (method === 'topic_starters.control') {
        current = { ...current, enabled: params.action === 'enable' };
        return current;
      }
      if (method === 'topic_starters.select_model') {
        current = { ...current, selected_model: params.model };
        return current;
      }
      if (method === 'topic_starters.profile') return profile;
      if (method === 'topic_starters.get') return { available: false, reason: 'v6_pool_miss', policy_id: 'topic-starter-mvp-v6' };
      if (method === 'topic_starters.restore_direction' && restoredProfile) return restoredProfile;
      throw new Error(`Unexpected request: ${method}`);
    };
    const root = createRoot(document.getElementById('root'));
    t.after(async () => {
      await act(async () => root.unmount());
    });
    await act(async () =>
      root.render(
        createElement(
          I18nextProvider,
          { i18n },
          createElement(
            SettingsServicesProvider,
            {
              isConnected: true,
              connectionState: 'connected',
              request: async () => ({}),
              recommendationFocus: focus,
              onCorrectRecommendation: (text) => corrections.push(text),
            },
            createElement('section', null, createElement(RecommendationSettings, { disabled: false })),
          ),
        ),
      ),
    );
    await settle();
    return {
      requests,
      corrections,
      controls: () => requests.filter(({ method }) => method === 'topic_starters.control'),
      selections: () => requests.filter(({ method }) => method === 'topic_starters.select_model').map(({ params }) => params),
    };
  }

  const rowTitles = () => [...document.querySelectorAll('.setting-row__title')].map((node) => node.textContent);
  const openDialogs = () => [...document.querySelectorAll('dialog')].filter((dialog) => dialog.open);
  const alerts = () => [...document.querySelectorAll('[role="alert"]')].map((node) => node.textContent);
  const modelSelect = () => document.querySelector('[aria-label="话题推荐模型"]');
  const modelOptions = () => [...document.querySelectorAll('[role="option"]')];

  function buttonIn(container, label) {
    const button = [...container.querySelectorAll('button')].find((node) => node.textContent.trim() === label);
    assert.equal(button === undefined, false, `Button not found: ${label}`);
    return button;
  }

  async function click(element) {
    await act(async () => element.click());
    await settle();
  }

  async function type(element, value) {
    await act(async () => {
      Object.getOwnPropertyDescriptor(dom.window.HTMLTextAreaElement.prototype, 'value').set.call(element, value);
      element.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    });
  }

  test('recommendation is its own page after knowledge, and agent and models do not host it', () => {
    const definition = readFileSync(new URL('../src/features/settings/registry/openSourceDefinition.ts', import.meta.url), 'utf8');
    assert.match(definition, /personalContextSettingsModule,\s*recommendationModule,/);
    assert.equal(recommendationModule.id, 'recommendation');
    assert.equal(recommendationModule.titleKey, 'settingsPanel.categories.recommendation');
    assert.equal(recommendationModule.sections.length, 1);
    assert.equal(recommendationModule.sections[0].separatedRows, true);
    const items = recommendationModule.sections[0].items;
    assert.equal(items[0].component, 'switch');
    assert.equal(items[0].key, 'topic_recommendation_enabled');
    assert.equal(items[1].component, 'switch');
    assert.equal(items[1].key, 'prompt_suggestion_enabled');
    assert.equal(items[2].component, 'custom');
    assert.equal(items[2].render === RecommendationSettings, true);
    assert.equal(agentModule.sections.some((section) => section.id === 'recommendation'), false);
    const modelItems = modelsModule.sections.flatMap((section) => section.items.map((item) => item.id));
    assert.equal(modelItems.some((id) => id.includes('recommendation')), false);
  });

  test('choosing a model sends only its ID and shows it as chosen', async (t) => {
    const ui = await mount(t);
    await click(modelSelect());
    await click(modelOptions()[2]);
    assert.deepEqual(ui.selections(), [{ model: 'mdl_pro' }]);
    assert.equal(modelSelect()?.textContent, 'deepseek-v4-1-pro');

    await click(modelSelect());
    await click(modelOptions()[3]);
    assert.equal(ui.selections().length, 1);
  });

  test('viewing the data opens the profile with its sources and correction', async (t) => {
    const ui = await mount(t, {
      status: { ...READY_STATUS, enabled: true },
      profile: {
        units: [{
          unit_id: 'unit-1',
          summary: '想学国际象棋残局',
          sources: [{ session_id: 's1', ordinal: 0, content: '车兵残局总是赢不下来' }],
        }],
      },
    });
    assert.equal(ui.requests.some(({ method }) => method === 'topic_starters.profile'), false);
    await click(buttonIn(document, '查看'));
    assert.equal(ui.requests.filter(({ method }) => method === 'topic_starters.profile').length, 1);
    assert.equal(document.body.textContent.includes('想学国际象棋残局'), true);
    assert.equal(document.body.textContent.includes('车兵残局总是赢不下来'), true);
    await type(document.querySelector('textarea'), '不再推荐残局');
    await click(buttonIn(document, '在新对话中纠正'));
    assert.deepEqual(ui.corrections, ['不再推荐残局']);
    await click(buttonIn(document, '收起'));
    assert.equal(document.body.textContent.includes('想学国际象棋残局'), false);
  });

  test('deleting the data asks for confirmation and then clears it', async (t) => {
    const ui = await mount(t, { status: { ...READY_STATUS, enabled: true } });
    const dataRow = [...document.querySelectorAll('.setting-row')].find(
      (row) => row.querySelector('.setting-row__title').textContent === '推荐数据',
    );
    await click(buttonIn(dataRow, '删除'));
    assert.equal(openDialogs().length, 1);
    const [dialog] = openDialogs();
    assert.equal(dialog.textContent.includes('原始聊天保留'), true);
    assert.equal(ui.controls().length, 0);
    await click(buttonIn(dialog, '删除'));
    assert.deepEqual(ui.controls().map(({ params }) => params.action), ['delete']);
    assert.equal(openDialogs().length, 0);
  });

});

// ChatPanel: welcome cards and in-conversation suggestion chips.
const CACHE = '../node_modules/.cache/topic-starter-model-handoff';
const MODEL_STORAGE_KEY = 'jiuwenclaw_selected_model';
const SESSION = 'web_1a0f7acc572_4c3a3563ebed';
const OTHER_SESSION = 'web_1a0f7afa412_50e95c15fa86';
const SUGGESTION = '把第 5 段写成 4 分钟的结尾逐字稿';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost:5173/',
});
// The page is in the foreground unless a test sends it to the background.
let pageVisibility = 'visible';
Object.defineProperty(dom.window.document, 'visibilityState', { configurable: true, get: () => pageVisibility });
Object.defineProperty(dom.window.document, 'hidden', { configurable: true, get: () => pageVisibility !== 'visible' });

const sent = [];
const starterEventVisibleTitles = [];
let liveSocket = null;
let holdStarterResponses = false;
const starterEventResponses = new Map();
let recommendationEnabled = true;

const V6_UNAVAILABLE_PAYLOAD = {
  available: false,
  reason: 'pool_empty_lazy_seed_started',
  policy_id: 'topic-starter-mvp-v6',
};

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;

  constructor(url) {
    this.url = url;
    this.readyState = FakeWebSocket.OPEN;
    liveSocket = this;
    queueMicrotask(() => this.onopen && this.onopen({}));
  }

  send(data) {
    const frame = JSON.parse(data);
    sent.push(frame);

    if (frame.method === 'prompt_suggestion.event') {
      queueMicrotask(() => this.reply(frame.id, { recorded: true }));
    } else if (frame.method === 'topic_starters.status' || frame.method === 'topic_starters.control') {
      if (frame.method === 'topic_starters.control') recommendationEnabled = frame.params.action === 'enable';
      queueMicrotask(() => this.reply(frame.id, {
        enabled: recommendationEnabled, configured: true, last_failure: null,
        selected_model: 'follow_main', model_options: [],
      }));
    } else if (frame.method === 'topic_starters.event') {
      starterEventVisibleTitles.push({
        eventType: frame.params.event_type,
        slateId: frame.params.slate_id,
        titles: [...document.querySelectorAll('[data-testid="chat-panel-welcome-suggestion"]')].map(el => el.getAttribute('data-variant')),
      });
      const recorded = starterEventResponses.get(frame.params.event_type);
      if (recorded !== null) {
        queueMicrotask(() => this.reply(frame.id, typeof recorded === 'object' ? recorded : { recorded: recorded ?? true }));
      }
    } else if (frame.method === 'topic_starters.get' && !holdStarterResponses) {
      queueMicrotask(() => this.reply(frame.id, V6_UNAVAILABLE_PAYLOAD));
    } else if (frame.method === 'topic_starters.foreground') {
      queueMicrotask(() => this.reply(frame.id, { scheduled: false }));
    }
  }

  reply(id, payload) {
    this.onmessage && this.onmessage({ data: JSON.stringify({ type: 'res', id, ok: true, payload }) });
  }

  close() {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose && this.onclose({ code: 1000, reason: '' });
  }
}

const NOOP_OBSERVER = class {
  observe() {}
  unobserve() {}
  disconnect() {}
};

let chipOutOfView = false;
const outOfView = new Set();
class ScreenObserver {
  constructor(callback) {
    this.callback = callback;
  }

  observe(target) {
    this.target = target;
    if (chipOutOfView) outOfView.add(this);
    else queueMicrotask(() => this.callback([{ target, isIntersecting: true, intersectionRatio: 1 }], this));
  }

  unobserve() {}

  disconnect() {
    outOfView.delete(this);
  }
}


for (const [name, value] of Object.entries({
  window: dom.window,
  document: dom.window.document,
  navigator: dom.window.navigator,
  localStorage: dom.window.localStorage,
  location: dom.window.location,
  HTMLElement: dom.window.HTMLElement,
  CustomEvent: dom.window.CustomEvent,
  Node: dom.window.Node,
  Element: dom.window.Element,
  getComputedStyle: dom.window.getComputedStyle,
  requestAnimationFrame: cb => setTimeout(cb, 0),
  cancelAnimationFrame: id => clearTimeout(id),
  ResizeObserver: NOOP_OBSERVER,
  IntersectionObserver: ScreenObserver,
  matchMedia: () => ({
    matches: false,
    addEventListener() {},
    removeEventListener() {},
    addListener() {},
    removeListener() {},
  }),
  WebSocket: FakeWebSocket,
  __viteGlob: () => ({}),
})) {
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
}
dom.window.WebSocket = FakeWebSocket;
dom.window.ResizeObserver = NOOP_OBSERVER;
dom.window.IntersectionObserver = ScreenObserver;
dom.window.matchMedia = globalThis.matchMedia;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

for (const [name, value] of Object.entries({ offsetHeight: 2000, offsetWidth: 800, clientHeight: 2000, clientWidth: 800 })) {
  Object.defineProperty(dom.window.HTMLElement.prototype, name, { configurable: true, get: () => value });
}

// The app picks its language from localStorage only; these assertions use the English strings.
dom.window.localStorage.setItem('i18nextLng', 'en');
const react = await import('react');
const { createRoot } = await import('react-dom/client');
const { ChatPanel } = await import(`${CACHE}/components/ChatPanel/index.js`);
const { usePromptSuggestionBox } = await import(`${CACHE}/hooks/usePromptSuggestionBox.js`);
const { ensureSessionRuntimes, resolveEffectiveModel, useChatStore, useSessionStore, useWorkspaceStore } = await import(`${CACHE}/stores/index.js`);

const { A2UIProvider } = await import('@a2ui/react');
const { webClient } = await import(`${CACHE}/services/webClient.js`);

const PANEL_PROPS = {
  onSendMessage: () => {},
  onPersistMedia: async () => ({}),
  onPersistDocuments: async () => ({}),
  onInterrupt: () => {},
  onCancel: () => {},
  onSwitchMode: () => {},
  isProcessing: false,
  isConnected: true,
  onUserAnswer: () => {},
  permissionsEnabled: false,
  onSavePermission: async () => {},
};

const settle = () => new Promise(resolve => setTimeout(resolve, 30));

function starterRequests() {
  return sent.filter(frame => frame.method === 'topic_starters.get');
}

function starterEvents(eventType = null) {
  return sent.filter(frame => frame.method === 'topic_starters.event' && (eventType === null || frame.params.event_type === eventType));
}

function visibleSuggestionTitles() {
  return [...dom.window.document.querySelectorAll('[data-testid="chat-panel-welcome-suggestion"]')].map(el => el.getAttribute('data-variant'));
}

function dynamicSlateResponse(sessionId, tag, count = 4, projectId = null) {
  const batchId = `batch-${tag}`;
  const contextScope = projectId ? `project:${projectId}` : 'global_new_chat';
  return {
    available: true,
    slate_id: `slate-${tag}`,
    manifest_id: `manifest-${tag}`,
    generation_batch_id: batchId,
    session_id: sessionId,
    profile_version: 1,
    policy_id: 'topic-starter-mvp-v6',
    context_scope: contextScope,
    project_id: projectId,
    short_intent_version: 2,
    pool_card_count: projectId ? 4 : 7,
    exposure_slice_id: `slice-${tag}`,
    selection_mode: count < 3 ? 'mixed_dynamic' : 'personalized_dynamic',
    topics: Array.from({ length: count }, (_, index) => ({
      id: `${tag}-revision-${index}`,
      card_revision_id: `${tag}-revision-${index}`,
      card_id: `${tag}-card-${index}`,
      generation_batch_id: batchId,
      position: index,
      title: `${tag}-title-${index}`,
      prompt: `${tag}-prompt-${index}`,
    })),
  };
}

async function mountWelcome(
  sessionId,
  { holdResponses = false, staleAlias = null, onSendMessage = () => {}, topicStarterProjectId = null, isConnected = true, visibility = 'visible', onRender = null } = {},
) {
  pageVisibility = visibility;
  sent.length = 0;
  starterEventVisibleTitles.length = 0;
  holdStarterResponses = holdResponses;
  starterEventResponses.clear();
  dom.window.localStorage.clear();
  dom.window.localStorage.setItem('i18nextLng', 'en');
  if (staleAlias) dom.window.localStorage.setItem(MODEL_STORAGE_KEY, staleAlias);
  useSessionStore.setState({ availableModels: [], chatAvailableModels: [], defaultModelName: null });
  ensureSessionRuntimes(sessionId);
  useChatStore.getState().setActiveSessionId(sessionId);

  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  const renderProps = {
    ...PANEL_PROPS,
    onSendMessage,
    topicStarterProjectId,
    isConnected,
  };
  function WelcomePage() {
    const sessionId = useChatStore(s => s.activeSessionId);
    const promptSuggestionBox = usePromptSuggestionBox(sessionId, false, true);
    const panel = react.createElement(ChatPanel, { ...renderProps, promptSuggestionBox });
    return onRender ? react.createElement(react.Profiler, { id: 'chat-panel-welcome', onRender }, panel) : panel;
  }
  const renderPanel = () => react.createElement(WelcomePage);
  await react.act(async () => {
    root.render(renderPanel());
  });
  await react.act(settle);
  return {
    rerender: async updates => {
      Object.assign(renderProps, updates);
      await react.act(async () => {
        root.render(renderPanel());
      });
      await react.act(settle);
    },
    unmount: async () => {
      await react.act(async () => {
        root.unmount();
      });
    },
  };
}

async function setPageVisibility(state) {
  await react.act(async () => {
    pageVisibility = state;
    dom.window.document.dispatchEvent(new dom.window.Event('visibilitychange'));
  });
  await react.act(settle);
}


function noticeText() {
  return dom.window.document.querySelector('[data-testid="chat-panel-topic-starter-dismiss-notice-text"]')?.textContent ?? null;
}

function noticeButtons() {
  return [...dom.window.document.querySelectorAll('[data-testid="chat-panel-topic-starter-dismiss-notice"] button')].map(button => button.textContent);
}

function scopeParams(request) {
  const { visit_id, manual_retry, ...scope } = request.params;
  assert.match(visit_id, /^topic-visit-/);
  assert.equal(manual_retry, false);
  return scope;
}

test('a pool that answers at once is shown without a static entrance first', async () => {
  const panel = await mountWelcome('sess-no-flash', { holdResponses: true });
  try {
    // While the pool read is pending the page shows no cards rather than generic ones it would swap out.
    assert.deepEqual(visibleSuggestionTitles(), []);
    await react.act(async () => {
      liveSocket.reply(starterRequests()[0].id, dynamicSlateResponse('sess-no-flash', 'QUICK', 4));
    });
    await react.act(settle);
    assert.deepEqual(visibleSuggestionTitles(), ['QUICK-title-0', 'QUICK-title-1', 'QUICK-title-2', 'QUICK-title-3']);
  } finally {
    await panel.unmount();
  }
});

test('project new-chat requests and accepts only its selected project pool', async () => {
  const panel = await mountWelcome('sess-project', {
    holdResponses: true,
    topicStarterProjectId: 'project-7',
  });
  try {
    const request = starterRequests()[0];
    assert.deepEqual(scopeParams(request), {
      session_id: 'sess-project',
      policy_id: 'topic-starter-mvp-v6',
      context_scope: 'project:project-7',
      project_id: 'project-7',
    });
    await react.act(async () => {
      liveSocket.reply(request.id, dynamicSlateResponse('sess-project', 'PROJECT', 4, 'project-7'));
    });
    await react.act(settle);
    assert.deepEqual(visibleSuggestionTitles(), ['PROJECT-title-0', 'PROJECT-title-1', 'PROJECT-title-2', 'PROJECT-title-3']);
  } finally {
    await panel.unmount();
  }
});

test('clicking a v6 card keeps the session effective model for the normal answer', async () => {
  const sentMessages = [];
  const panel = await mountWelcome('sess-answer-model', {
    holdResponses: true,
    onSendMessage: async (prompt, mediaItems, provenance) => {
      sentMessages.push({ prompt, mediaItems, provenance });
      return 'sess-answer-model';
    },
  });
  try {
    await react.act(async () => {
      useSessionStore.getState().setAvailableModels(
        [
          { model_name: 'model-a', alias: 'A', is_default: true },
          { model_name: 'model-b', alias: 'B', is_default: true },
        ],
        'model-a',
      );
      useSessionStore.getState().setSelectedModelName('sess-answer-model', 'model-b');
      liveSocket.reply(starterRequests()[0].id, dynamicSlateResponse('sess-answer-model', 'ANSWER', 4));
    });
    await react.act(settle);

    const before = useSessionStore.getState();
    assert.equal(
      resolveEffectiveModel(before.chatAvailableModels, before.runtimes['sess-answer-model'].selectedModelName, before.defaultModelName)?.model_name,
      'model-b',
    );

    const cardButton = dom.window.document.querySelector('.chat-suggestion-card__main');
    assert.ok(cardButton);
    await react.act(async () => {
      cardButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    await react.act(settle);

    assert.deepEqual(sentMessages, [
      {
        prompt: 'ANSWER-prompt-0',
        mediaItems: undefined,
        provenance: {
          input_origin: 'starter_click',
          assistance_origin: 'starter_click',
          topic_starter: {
            manifest_id: 'manifest-ANSWER',
            generation_batch_id: 'batch-ANSWER',
            exposure_id: 'slate-ANSWER',
            card_revision_id: 'ANSWER-revision-0',
            context_scope: 'global_new_chat',
            project_id: null,
          },
        },
      },
    ]);
    const after = useSessionStore.getState();
    assert.equal(
      resolveEffectiveModel(after.chatAvailableModels, after.runtimes['sess-answer-model'].selectedModelName, after.defaultModelName)?.model_name,
      'model-b',
    );
    assert.equal(Object.hasOwn(starterRequests()[0].params, 'model_name'), false);
  } finally {
    await panel.unmount();
  }
});

test('not interested keeps the card until the server confirms it was recorded', async () => {
  const panel = await mountWelcome('sess-dislike', { holdResponses: true });
  try {
    await react.act(async () => {
      liveSocket.reply(starterRequests()[0].id, dynamicSlateResponse('sess-dislike', 'DISLIKE', 4));
    });
    await react.act(settle);
    starterEventResponses.set('not_interested', null);

    const dismissButton = dom.window.document.querySelector('.chat-suggestion-card__dismiss');
    assert.ok(dismissButton);
    await react.act(async () => {
      dismissButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    await react.act(settle);
    assert.equal(visibleSuggestionTitles().length, 4);
    await react.act(async () => {
      dismissButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    await react.act(settle);
    assert.equal(starterEvents('not_interested').filter(frame => frame.params.slate_id === 'slate-DISLIKE').length, 1);

    const rejectedEvent = starterEvents('not_interested').at(-1);
    assert.ok(rejectedEvent);
    await react.act(async () => {
      liveSocket.reply(rejectedEvent.id, { recorded: false });
    });
    await react.act(settle);
    assert.equal(visibleSuggestionTitles().length, 4);
    assert.equal(dom.window.document.querySelector('[data-testid="chat-panel-topic-starter-dismiss-notice"]') === null, true);

    starterEventResponses.set('not_interested', true);
    await react.act(async () => {
      dismissButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    await react.act(settle);
    assert.equal(visibleSuggestionTitles().length, 3);

    const remainingCardButton = dom.window.document.querySelector('.chat-suggestion-card__main');
    assert.ok(remainingCardButton);
    await react.act(async () => {
      remainingCardButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    await react.act(settle);
    assert.equal(starterEvents('dynamic_exposure').filter(frame => frame.params.slate_id === 'slate-DISLIKE').length, 1);
  } finally {
    await panel.unmount();
  }
});

test('a confirmed dismissal keeps the remaining cards on screen while the pool backfills', async () => {
  const panel = await mountWelcome('sess-backfill', { holdResponses: true });
  try {
    await react.act(async () => {
      liveSocket.reply(starterRequests()[0].id, dynamicSlateResponse('sess-backfill', 'BACKFILL', 4));
    });
    await react.act(settle);
    starterEventResponses.set('not_interested', { recorded: true, pausable: true });

    const dismissButton = dom.window.document.querySelector('.chat-suggestion-card__dismiss');
    assert.ok(dismissButton);
    assert.equal(['Skip for now', '先跳过'].includes(dismissButton.getAttribute('title')), true);
    await react.act(async () => {
      dismissButton.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    await react.act(settle);

    // The page re-reads the pool after the dismissal; until it answers the other three stay.
    assert.equal(starterRequests().length, 2);
    assert.deepEqual(visibleSuggestionTitles(), ['BACKFILL-title-1', 'BACKFILL-title-2', 'BACKFILL-title-3']);
    const notice = () => dom.window.document.querySelector('[data-testid="chat-panel-topic-starter-dismiss-notice"]');
    assert.equal(['Skipped for now', '这条先不看了'].includes(noticeText()), true);
    assert.equal(
      JSON.stringify(noticeButtons()) === JSON.stringify(['Show this less for a week', 'Undo'])
        || JSON.stringify(noticeButtons()) === JSON.stringify(['这类先藏一周', '撤销']),
      true,
    );
    const heading = dom.window.document.querySelector('[data-testid="chat-panel-welcome-recommendation-label"]');
    assert.equal(heading?.nextElementSibling?.getAttribute('data-testid'), 'chat-panel-topic-starter-dismiss-notice');

    await react.act(async () => {
      liveSocket.reply(starterRequests()[1].id, dynamicSlateResponse('sess-backfill', 'REFILL', 4));
    });
    await react.act(settle);
    assert.deepEqual(visibleSuggestionTitles(), ['REFILL-title-0', 'REFILL-title-1', 'REFILL-title-2', 'REFILL-title-3']);
    assert.equal(starterEvents('dynamic_exposure').filter(frame => frame.params.slate_id === 'slate-REFILL').length, 1);
    assert.equal(['Skipped for now', '这条先不看了'].includes(noticeText()), true);

    // The notice stays eight seconds so its buttons can be used, then leaves the page; the cards stay.
    await react.act(async () => { await new Promise(resolve => setTimeout(resolve, 4100)); });
    assert.equal(['Skipped for now', '这条先不看了'].includes(noticeText()), true);
    await react.act(async () => { await new Promise(resolve => setTimeout(resolve, 4000)); });
    assert.equal(notice() === null, true);
    assert.deepEqual(visibleSuggestionTitles(), ['REFILL-title-0', 'REFILL-title-1', 'REFILL-title-2', 'REFILL-title-3']);
  } finally {
    await panel.unmount();
  }
});

function openConversation(sessionId) {
  ensureSessionRuntimes(sessionId);
  const store = useChatStore.getState();
  if (!store.runtimes[sessionId].messages.length) {
    store.addMessage(sessionId, { id: `${sessionId}-u1`, role: 'user', content: '把讲稿压到二十分钟', timestamp: '2026-10-09T08:00:00Z' });
    store.addMessage(sessionId, { id: `${sessionId}-a1`, role: 'assistant', content: '压缩后的讲稿分五段。', timestamp: '2026-10-09T08:00:05Z' });
  }
  store.setInputValue(sessionId, '');
  store.setProcessing(sessionId, false);
}

function ConversationPage({ pageActive, panelProps }) {
  const sessionId = useChatStore(s => s.activeSessionId);
  const hasConversation = useChatStore(s => Boolean(s.runtimes[s.activeSessionId ?? '']?.messages.length));
  const promptSuggestionBox = usePromptSuggestionBox(sessionId, hasConversation, pageActive);
  react.useEffect(() => {
    promptSuggestionBox.setEnabled(true);
  }, [promptSuggestionBox.setEnabled]);
  // The page renders replies, which need the A2UI provider main.tsx puts around the app.
  return pageActive
    ? react.createElement(A2UIProvider, null, react.createElement(ChatPanel, { ...panelProps, promptSuggestionBox }))
    : react.createElement('button', {
      type: 'button',
      'data-testid': 'settings-prompt-suggestion-enabled',
      onClick: () => promptSuggestionBox.setEnabled(false),
    });
}

// A conversation page whose last answer has completed. ``sends`` collects what the panel hands to App.
async function mountConversation({ onSendMessage, completeInitial = true } = {}) {
  sent.length = 0;
  chipOutOfView = false;
  recommendationEnabled = true;
  dom.window.localStorage.setItem('i18nextLng', 'en');
  useSessionStore.setState({ availableModels: [], chatAvailableModels: [], defaultModelName: null });
  openConversation(SESSION);
  openConversation(OTHER_SESSION);
  useChatStore.getState().setActiveSessionId(SESSION);
  const sends = [];
  const root = createRoot(dom.window.document.getElementById('root'));
  const props = {
    onSendMessage: async (content, mediaItems, provenance) => {
      sends.push({ content, provenance });
      return onSendMessage ? onSendMessage(content) : SESSION;
    },
    onPersistMedia: async () => ({}),
    onPersistDocuments: async () => ({}),
    onInterrupt: () => {},
    onCancel: () => {},
    onSwitchMode: () => {},
    isProcessing: false,
    isConnected: true,
    onUserAnswer: () => {},
    permissionsEnabled: false,
    onSavePermission: async () => {},
  };
  let pageActive = true;
  const renderPage = () => react.createElement(ConversationPage, { pageActive, panelProps: props });
  await react.act(async () => {
    root.render(renderPage());
    await webClient.connect();
  });
  await react.act(settle);
  if (completeInitial) {
    await completeRequest(SESSION);
    await completeRequest(OTHER_SESSION);
  }
  return {
    sends,
    setPageActive: async active => {
      pageActive = active;
      await react.act(async () => {
        root.render(renderPage());
        await settle();
      });
    },
    navigate: async (sessionId, projectId) => {
      props.topicStarterProjectId = projectId;
      await react.act(async () => {
        useChatStore.getState().setActiveSessionId(sessionId);
        root.render(renderPage());
        await settle();
      });
    },
    unmount: async () => {
      await react.act(async () => root.unmount());
    },
  };
}

const ACCEPTED_ORIGIN = { input_origin: 'prompt_suggestion', assistance_origin: 'prompt_suggestion' };

let suggestionSequence = 0;
let requestSequence = 0;
const completedRequests = new Map();

async function completeRequest(sessionId, requestId = `req_mupkt7y0_${++requestSequence}`) {
  completedRequests.set(sessionId, requestId);
  await react.act(async () => {
    liveSocket.onmessage({ data: JSON.stringify({
      type: 'event', event: 'chat.final',
      payload: { session_id: sessionId, request_id: requestId, content: '回答完成', completed_at: '2026-10-12T08:00:00Z' },
    }) });
    await settle();
  });
  return requestId;
}

// A request starts in the session as the gateway reports it, and the page turns busy.
async function startRequest(sessionId, requestId = `req_mupkt7y0_${++requestSequence}`) {
  await react.act(async () => {
    liveSocket.onmessage({ data: JSON.stringify({
      type: 'event', event: 'chat.processing_status',
      payload: { event_type: 'chat.processing_status', session_id: sessionId, request_id: requestId, is_processing: true, is_complete: false },
    }) });
    useChatStore.getState().setProcessing(sessionId, true);
    await settle();
  });
  return requestId;
}

async function pushSuggestion({ sessionId = SESSION, text = SUGGESTION, requestId = completedRequests.get(sessionId) } = {}) {
  suggestionSequence += 1;
  const suggestionId = `9c3f1e0a5b7d4c2e8f6a1b3d5e7f${String(suggestionSequence).padStart(4, '0')}`;
  await react.act(async () => {
    liveSocket.onmessage({
      data: JSON.stringify({
        type: 'event',
        event: 'prompt_suggestion.ready',
        payload: { event_type: 'prompt_suggestion.ready', session_id: sessionId, request_id: requestId, suggestion_id: suggestionId, text },
      }),
    });
    await settle();
  });
  return suggestionId;
}

async function scrollChipIntoView() {
  await react.act(async () => {
    chipOutOfView = false;
    for (const observer of [...outOfView]) observer.callback([{ target: observer.target, isIntersecting: true, intersectionRatio: 1 }], observer);
    await settle();
  });
}

// The timeline's scroll root with a content height and scroll position the test drives; jsdom lays
// nothing out, and every element reports a client height of 2000.
function scrollRoot(height) {
  const el = dom.window.document.querySelector('[data-testid="chat-panel-scroll"]');
  let top = 0;
  Object.defineProperty(el, 'scrollHeight', { configurable: true, get: () => height.value });
  Object.defineProperty(el, 'scrollTop', { configurable: true, get: () => top, set: value => { top = value; } });
  return el;
}

// A scroll event, after the reader moved to ``top`` or with the position unchanged.
async function scrollEvent(el, top = el.scrollTop) {
  await react.act(async () => {
    el.scrollTop = top;
    el.dispatchEvent(new dom.window.Event('scroll'));
    await settle();
  });
}

function box() {
  return dom.window.document.querySelector('[data-testid="chat-panel-prompt-suggestion-chip"]');
}

function editor() {
  return dom.window.document.querySelector('[data-testid="chat-panel-input"]');
}

// [event_type, suggestion_id, method or reason] of every outcome the page recorded.
function outcomes() {
  return sent
    .filter(frame => frame.method === 'prompt_suggestion.event' && frame.params.event_type !== 'input_changed')
    .map(frame => [frame.params.event_type, frame.params.suggestion_id, frame.params.method ?? frame.params.reason ?? null]);
}

async function setComposer(sessionId, value) {
  await react.act(async () => {
    useChatStore.getState().setInputValue(sessionId, value);
    dom.window.dispatchEvent(new dom.window.CustomEvent('chat-input-sync', { detail: { sessionId, value } }));
    await settle();
  });
}

async function setProcessing(sessionId, value) {
  await react.act(async () => {
    useChatStore.getState().setProcessing(sessionId, value);
    await settle();
  });
}

async function clickTestId(testId) {
  await react.act(async () => {
    dom.window.document.querySelector(`[data-testid="${testId}"]`).click();
    await settle();
  });
}

// Dispatches a key on the composer, where the user's focus is; returns whether the page took the key.
async function pressInComposer(key, options = {}) {
  let event;
  await react.act(async () => {
    event = new dom.window.KeyboardEvent('keydown', { key, bubbles: true, cancelable: true, ...options });
    editor().dispatchEvent(event);
    await settle();
  });
  return event.defaultPrevented;
}

test('disabling recommendation in settings discards suggestions that have never shown', async () => {
  const panel = await mountConversation();
  try {
    await panel.setPageActive(false);
    await pushSuggestion();
    await clickTestId('settings-prompt-suggestion-enabled');
    await panel.setPageActive(true);
    assert.equal(box() === null, true);
    assert.equal(outcomes().length, 0);
  } finally {
    await panel.unmount();
  }
});

test('clicking the chip sends the suggestion with its provenance and leaves the composer empty', async () => {
  const panel = await mountConversation();
  try {
    const id = await pushSuggestion();
    await clickTestId('chat-panel-prompt-suggestion-chip-send');
    assert.equal(useChatStore.getState().runtimes[SESSION].inputValue, '');
    assert.equal(box() === null, true);
    assert.deepEqual(panel.sends, [
      { content: SUGGESTION, provenance: { ...ACCEPTED_ORIGIN, prompt_suggestion: { suggestion_id: id, edited: false } } },
    ]);
    const [shown, accepted] = sent.filter(frame => frame.method === 'prompt_suggestion.event').map(frame => frame.params);
    assert.deepEqual([shown.event_type, accepted.event_type, accepted.suggestion_id, accepted.session_id, accepted.method],
      ['shown', 'accepted', id, SESSION, 'click']);
    assert.equal(Number.isInteger(accepted.time_to_accept_ms) && accepted.time_to_accept_ms >= 0, true);
    assert.equal(sent.some(frame => frame.method === 'prompt_suggestion.event' && frame.params.event_type === 'input_changed'), false);
  } finally {
    await panel.unmount();
  }
});

test('Tab in a composer with text leaves the users draft alone', async () => {
  const panel = await mountConversation();
  try {
    await pushSuggestion();
    await setComposer(SESSION, '我想先问别的');
    assert.equal(await pressInComposer('Tab'), false);
    assert.equal(useChatStore.getState().runtimes[SESSION].inputValue, '我想先问别的');
    assert.deepEqual(outcomes().map(([eventType]) => eventType), ['shown', 'ignored']);
  } finally {
    await panel.unmount();
  }
});

async function send() {
  await clickTestId('chat-panel-input-send');
}

// The request a send started runs and completes.
async function answer() {
  await completeRequest(SESSION, await startRequest(SESSION));
  await setProcessing(SESSION, false);
}


test('two suggestions for the same reply show together and sending one ignores the other', async () => {
  const panel = await mountConversation();
  try {
    const first = await pushSuggestion({ text: '买ETF，长期配置' });
    const second = await pushSuggestion({ text: '对比一下金条和ETF的费用' });
    const chips = [...dom.window.document.querySelectorAll('[data-testid="chat-panel-prompt-suggestion-chip-send"]')];
    assert.deepEqual(chips.map(chip => chip.getAttribute('data-variant')), [first, second]);
    assert.equal(chips[0].textContent.includes('买ETF，长期配置'), true);
    assert.equal(chips[1].textContent.includes('对比一下金条和ETF的费用'), true);
    assert.equal(dom.window.document.querySelectorAll('[data-testid="chat-panel-prompt-suggestion-chip-dismiss"]').length, 2);
    await react.act(async () => {
      chips[1].click();
      await settle();
    });
    assert.equal(box() === null, true);
    assert.deepEqual(panel.sends, [
      { content: '对比一下金条和ETF的费用', provenance: { ...ACCEPTED_ORIGIN, prompt_suggestion: { suggestion_id: second, edited: false } } },
    ]);
    assert.deepEqual(outcomes(), [
      ['shown', first, null],
      ['shown', second, null],
      ['accepted', second, 'click'],
      ['ignored', first, 'sent_other'],
    ]);
  } finally {
    await panel.unmount();
  }
});
