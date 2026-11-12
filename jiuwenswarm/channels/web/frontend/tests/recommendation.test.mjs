import assert from 'node:assert/strict';
import registerTest from 'node:test';
import { JSDOM } from 'jsdom';

// Register only after all asynchronous UI imports finish; --test-force-exit can otherwise
// terminate the worker after the early pure tests, before the UI tests have been registered.
const cases = [];
const test = (...args) => cases.push(args);

import {
  buildTopicStarterRequest,
  buildDynamicTopicStarterSlate,
  buildDirectInputEvent,
  buildExposureEvent,
  buildPageLeftEvent,
  buildSessionBoundEvent,
  buildTopicClickEvent,
  topicStarterShowsProviderRefusal,
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
    work_mode: 'work',
    short_intent_version: 2,
    pool_card_count: 7,
    exposure_slice_id: 'slice-a',
    selection_mode: 'personalized_dynamic',
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
  workMode: 'work',
  shortIntentVersion: 2,
  exposureSliceId: 'slice-a',
  selectionMode: 'personalized_dynamic',
  topics: [topic(1), topic(2)],
};

test('collapses homepage recommendation status into two copy jobs', () => {
  assert.equal(topicStarterShowsStatus('ready', false), false);
  assert.equal(topicStarterShowsStatus('preparing', false), true);
  assert.equal(topicStarterShowsStatus('preparing', true), true);
  assert.equal(topicStarterShowsStatus('failed', false), false);
  assert.equal(topicStarterShowsStatus('no_content', false), false);
  assert.equal(topicStarterShowsStatus('waiting_for_input', false), false);
  assert.equal(topicStarterStatusMessageKey('preparing', false), 'chat.topicStarter.statusPreparing');
  assert.equal(topicStarterStatusMessageKey('preparing', true), 'chat.topicStarter.statusPreparing');
  assert.equal(topicStarterShowsSettingsAction('preparing'), false);
  assert.equal(topicStarterShowsStatus('disabled', true), true);
  assert.equal(topicStarterStatusMessageKey('disabled', true), 'chat.topicStarter.statusNeedsSettings');
  assert.equal(topicStarterStatusMessageKey('disabled', false), 'chat.topicStarter.statusNeedsSettings');
  assert.equal(topicStarterStatusMessageKey('missing_configuration', false), 'chat.topicStarter.statusNeedsSettings');
  assert.equal(topicStarterStatusMessageKey('provider_unavailable', false), 'chat.topicStarter.statusNeedsSettings');
  assert.equal(topicStarterShowsSettingsAction('disabled'), false);
  assert.equal(topicStarterShowsSettingsAction('failed'), false);
  assert.equal(topicStarterShowsProviderRefusal('provider_unavailable'), true);
  assert.equal(topicStarterShowsProviderRefusal('failed'), false);
});

test('builds isolated global and project new-chat requests', () => {
  assert.deepEqual(buildTopicStarterRequest('session-a', null), {
    session_id: 'session-a',
    policy_id: 'topic-starter-mvp-v6',
    context_scope: 'global_new_chat',
    project_id: null,
    work_mode: 'work',
  });
  assert.deepEqual(buildTopicStarterRequest('session-b', 'project-7'), {
    session_id: 'session-b',
    policy_id: 'topic-starter-mvp-v6',
    context_scope: 'project:project-7',
    project_id: 'project-7',
    work_mode: 'work',
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

test('click carries exact ids and no client-authored card content', () => {
  const click = buildTopicClickEvent(dynamicSlate, 'revision-2');
  assert.equal(click.event_type, 'topic_click');
  assert.equal(click.position, 1);
  assert.deepEqual(click.payload, {
    manifest_id: 'manifest-a',
    card_revision_id: 'revision-2',
    generation_batch_id: 'batch-a',
    context_scope: 'global_new_chat',
    project_id: null,
    work_mode: 'work',
    short_intent_version: 2,
    exposure_slice_id: 'slice-a',
  });
  for (const key of ['title', 'prompt', 'intent', 'evidence']) {
    assert.equal(Object.hasOwn(click.payload, key), false);
  }
});

test('keeps the exposure position after the slate is filtered', () => {
  const filteredSlate = {
    ...dynamicSlate,
    topics: [{ ...dynamicSlate.topics[1], position: 1 }],
  };
  const click = buildTopicClickEvent(filteredSlate, 'revision-2');
  assert.equal(click.position, 1);
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
dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
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

    if (frame.method === 'prompt_suggestion.get') {
      queueMicrotask(() => this.reply(frame.id, {
        enabled: recommendationEnabled, suggestions: recoveredPromptSuggestions.get(frame.params.session_id) ?? [],
      }));
    } else if (frame.method === 'prompt_suggestion.event') {
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
const { SettingsPage } = await import(`${CACHE}/features/settings/SettingsPage.js`);
const { openSourceSettingsPageDefinition } = await import(`${CACHE}/features/settings/registry/openSourceDefinition.js`);

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
    work_mode: 'work',
    short_intent_version: 2,
    pool_card_count: projectId ? 4 : 7,
    exposure_slice_id: `slice-${tag}`,
    selection_mode: 'personalized_dynamic',
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
  { holdResponses = false, staleAlias = null, onSendMessage = () => {}, topicStarterProjectId = null, isConnected = true, visibility = 'visible', onRender = null, onOpenGeneralSettings = () => {} } = {},
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
    onOpenGeneralSettings,
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


function scopeParams(request) {
  const { visit_id, ...scope } = request.params;
  assert.match(visit_id, /^topic-visit-/);
  assert.equal(Object.prototype.hasOwnProperty.call(request.params, 'manual_retry'), false);
  return scope;
}

test('downward slash suggestions raise the input container above welcome topics', async () => {
  const panel = await mountWelcome('new', { holdResponses: true });
  try {
    await react.act(async () => {
      const input = editor();
      input.focus();
      input.textContent = '/';
      const range = document.createRange();
      range.selectNodeContents(input);
      range.collapse(false);
      const selection = window.getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
      input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    });
    await react.act(settle);
    const menu = document.querySelector('[data-testid="chat-panel-composer-suggestion"]');
    assert.ok(menu?.classList.contains('chat-composer-suggestion--below'));
    const container = document.querySelector('[data-testid="chat-panel-input-container"]');
    assert.ok(container.classList.contains('chat-input-container--suggestion-open'));
    await pressInComposer('Escape');
    assert.equal(document.querySelector('[data-testid="chat-panel-composer-suggestion"]'), null);
    assert.equal(container.classList.contains('chat-input-container--suggestion-open'), false);
  } finally {
    await panel.unmount();
  }
});

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

test('General renders and persists the topic switch through config while keeping language on its locale source', async () => {
  assert.equal(openSourceSettingsPageDefinition.modules.some(module => module.id === 'recommendation'), false);
  const requests = [];
  let storedValue = 'false';
  let rejectSave = false;
  let modelConfig = {};
  const reasoning = { protocol_defaults: { openai: { options: [], recommended: null }, anthropic: { options: [], recommended: null } }, model_fallbacks: [] };
  const definition = {
    ...openSourceSettingsPageDefinition,
    modules: openSourceSettingsPageDefinition.modules.filter(module => module.id === 'general'),
  };
  const request = async (method, params) => {
    requests.push({ method, params });
    if (method === 'config.get') return { topic_recommendation_enabled: storedValue };
    if (method === 'config.save_all') {
      if (rejectSave) throw new Error('save rejected');
      storedValue = params.config.topic_recommendation_enabled;
      return { updated_env_keys: ['topic_recommendation_enabled'] };
    }
    if (method === 'locale.set_conf') return {};
    if (method === 'topic_starters.status') return {
      model_config: params?.include_api_key ? { ...modelConfig, api_key: 'saved-topic-key' } : modelConfig,
    };
    if (method === 'vendors.list') return { vendors: { reasoning, token_plan: [], coding_plan: [], custom_api: [] } };
    if (method === 'config.validate_model') return { ok: true, model_provider: 'OpenAI' };
    if (method === 'topic_starters.select_model') {
      if (params.configuration) modelConfig = { ...params.configuration, has_api_key: true };
      else { modelConfig = {}; storedValue = 'false'; }
      return {};
    }
    throw new Error(`Unexpected settings request: ${method}`);
  };
  const root = createRoot(document.getElementById('root'));
  let settingsPageRevision = 0;
  const render = async (connected = true) => {
    await react.act(async () => root.render(react.createElement(SettingsPage, {
      key: settingsPageRevision,
      definition, request, isConnected: connected, connectionState: connected ? 'ready' : 'disconnected',
    })));
    await react.act(settle);
  };
  const topicSwitch = () => document.querySelector('[data-testid="settings-switch"][data-variant="topic-recommendation-enabled"]');
  try {
    await render();
    assert.ok(document.querySelector('[data-testid="settings-select"][data-variant="preferred-language"]'));
    assert.equal(topicSwitch().getAttribute('aria-checked'), 'false');
    assert.equal(requests.some(request => request.method === 'locale.set_conf'), false);
    assert.equal(requests.some(request => request.method === 'models.list'), false);
    await react.act(async () => topicSwitch().click());
    await react.act(settle);
    assert.equal(storedValue, 'false', 'opening configuration must not enable recommendations');
    assert.ok(document.querySelector('[data-testid="settings-agent-media-config-dialog"]'));
    await react.act(async () => document.querySelector('[data-testid="settings-agent-media-config-dialog-cancel-btn"]').click());
    assert.equal(storedValue, 'false', 'cancelling must leave recommendations disabled');
    modelConfig = { model: 'topic-chat', provider: 'OpenAI', api_base: 'https://example.test/v1', has_api_key: true };
    rejectSave = true;
    await react.act(async () => topicSwitch().click());
    await react.act(settle);
    assert.equal(topicSwitch().getAttribute('aria-checked'), 'false', 'failed enable must keep the switch off');
    assert.equal(document.querySelector('[data-testid="settings-agent-media-config-dialog"]'), null);
    rejectSave = false;
    await react.act(async () => [...document.querySelectorAll('.settings-confirm-dialog')]
      .find(dialog => dialog.querySelector('.settings-confirm-dialog__message').textContent === 'save rejected')
      .querySelector('.settings-confirm-dialog__footer button:last-child').click());
    await react.act(settle);
    assert.equal(storedValue, 'true');
    assert.equal(topicSwitch().getAttribute('aria-checked'), 'true');
    assert.equal(document.querySelector('[data-testid="settings-topic-model-name"]').textContent, 'topic-chat');
    assert.equal(requests.some(request => request.method === 'topic_starters.select_model'), false);
    assert.equal(requests.filter(request => request.method === 'config.save_all').length, 2);
    assert.equal(requests.some(request => request.method === 'models.upsert'), false);
    assert.deepEqual(requests.find(request => request.method === 'config.save_all').params.config,
      { topic_recommendation_enabled: 'true' });
    rejectSave = true;
    await react.act(async () => topicSwitch().click());
    await react.act(settle);
    assert.equal(topicSwitch().getAttribute('aria-checked'), 'true', 'failed saves must roll back the visible switch');
    rejectSave = false;
    await react.act(async () => topicSwitch().click());
    await react.act(settle);
    assert.equal(storedValue, 'false');
    await react.act(async () => topicSwitch().click());
    await react.act(settle);
    assert.equal(storedValue, 'true', 'reenabling a saved model must save the switch directly');
    assert.equal(document.querySelector('[data-testid="settings-agent-media-config-dialog"]'), null);
    const testBtn = document.querySelector('[data-testid="settings-topic-model-test-btn"]');
    assert.ok(testBtn, 'saved topic model card must expose a test-connection button like chat models');
    const validateBeforeCardTest = requests.filter(request => request.method === 'config.validate_model').length;
    await react.act(async () => testBtn.click());
    await react.act(settle);
    const cardProbe = requests.findLast(request => request.method === 'config.validate_model');
    assert.ok(cardProbe, 'card test must call config.validate_model');
    assert.ok(requests.filter(request => request.method === 'config.validate_model').length > validateBeforeCardTest);
    assert.equal(cardProbe.params.model, 'topic-chat');
    assert.equal(cardProbe.params.api_key, 'saved-topic-key');
    assert.equal(cardProbe.params.api_base, 'https://example.test/v1');
    assert.ok(requests.some(request => request.method === 'topic_starters.status' && request.params?.include_api_key === true));
    assert.ok(document.querySelector('[data-testid="settings-topic-model-toast"][data-variant="success"]'));
    await react.act(async () => document.querySelector('[data-testid="settings-topic-model-edit"]').click());
    await react.act(settle);
    assert.ok(document.querySelector('[data-testid="settings-agent-media-config-dialog"]'));
    const savedKeyInput = document.querySelector('[data-testid="settings-agent-media-config-dialog-field"][data-variant="api_key"] input');
    assert.equal(savedKeyInput.value, 'saved-topic-key');
    assert.equal(savedKeyInput.type, 'password');
    assert.equal(requests.some(request => request.method === 'topic_starters.reveal_model_key'), false);
    assert.ok(requests.some(request => request.method === 'topic_starters.status' && request.params?.include_api_key === true));
    await react.act(async () => document.querySelector('[data-testid="settings-agent-media-config-dialog-save-btn"]').click());
    await react.act(settle);
    const probed = requests.findLast(request => request.method === 'config.validate_model');
    assert.ok(probed, 'topic save must probe with config.validate_model like chat models');
    assert.equal(probed.params.model, 'topic-chat');
    assert.equal(probed.params.api_key, 'saved-topic-key');
    const savedModel = requests.findLast(request => request.method === 'topic_starters.select_model');
    assert.equal(savedModel.params.configuration.api_key, 'saved-topic-key');
    assert.equal(savedModel.params.configuration.model, 'topic-chat');
    assert.ok(Number(savedModel.params.configuration.context_window_tokens) > 0);
    assert.equal('model' in savedModel.params, false, 'configuration saves must not also carry a clear-model sentinel');
    assert.equal(document.querySelector('[data-testid="settings-agent-media-config-dialog"]'), null);
    await react.act(async () => document.querySelector('[data-testid="settings-topic-model-edit"]').click());
    await react.act(settle);
    await render(false);
    const disconnectedDialog = [...document.querySelectorAll('.settings-confirm-dialog')]
      .find(dialog => dialog.textContent.includes('Disconnected. Reconnect and try again.'));
    assert.ok(disconnectedDialog, 'disconnect must replace loading with a closable error');
    await react.act(async () => disconnectedDialog.querySelector('.settings-confirm-dialog__close').click());
    await render(true);
    await react.act(async () => document.querySelector('[data-testid="settings-topic-model-delete"]').click());
    await react.act(async () => document.querySelector('.settings-confirm-dialog__footer button:last-child').click());
    await react.act(settle);
    assert.equal(topicSwitch().getAttribute('aria-checked'), 'false');
    assert.equal(document.querySelector('[data-testid="settings-topic-model-summary"]'), null);
    assert.equal(requests.some(request => request.method === 'models.delete'), false, 'unlinking recommendations must preserve the shared model');
    settingsPageRevision += 1;
    await render();
    assert.equal(storedValue, 'false', 'model deletion must persist the disabled switch on the backend');
    assert.equal(topicSwitch().getAttribute('aria-checked'), 'false', 'a fresh settings page must read the persisted disabled switch');
    assert.equal(document.querySelector('[data-testid="settings-topic-model-summary"]'), null);
  } finally {
    await react.act(async () => root.unmount());
  }
});

test('General ignores legacy chat models and opens the connection form without add-model fields', async () => {
  const requests = [];
  const root = createRoot(document.getElementById('root'));
  const request = async (method) => {
    requests.push(method);
    if (method === 'config.get') return { topic_recommendation_enabled: 'false' };
    if (method === 'topic_starters.status') return { selected_model: 'GLM-5.1', configured_model: 'GLM-5.1' };
    if (method === 'vendors.list') throw new Error('catalog offline');
    throw new Error(`Unexpected request ${method}`);
  };
  try {
    await react.act(async () => root.render(react.createElement(SettingsPage, {
      definition: { ...openSourceSettingsPageDefinition,
        modules: openSourceSettingsPageDefinition.modules.filter(module => module.id === 'general') },
      request, isConnected: true, connectionState: 'ready',
    })));
    await react.act(settle);
    assert.equal(document.querySelector('[data-testid="settings-topic-model-summary"]'), null);
    await react.act(async () => document.querySelector('[data-testid="settings-switch"][data-variant="topic-recommendation-enabled"]').click());
    await react.act(settle);
    const dialog = document.querySelector('[data-testid="settings-agent-media-config-dialog"]');
    assert.ok(dialog);
    assert.equal(dialog.querySelector('[data-variant="api_key"] input').placeholder.includes('saved'), false);
    for (const field of ['vendor_selection', 'protocol', 'api_key', 'model_name', 'context_window_tokens']) {
      assert.ok(dialog.querySelector(`[data-testid="settings-agent-media-config-dialog-field"][data-variant="${field}"]`));
    }
    for (const field of ['alias', 'reasoning_level']) {
      assert.equal(dialog.querySelector(`[data-variant="${field}"]`), null);
    }
    assert.equal(requests.includes('models.list'), false);
  } finally {
    await react.act(async () => root.unmount());
  }
});

test('disabled homepage keeps preset cards and links to General; enabling refreshes toward personalized cards', async () => {
  let settingsOpens = 0;
  const panel = await mountWelcome('sess-topic-switch', {
    holdResponses: true, onOpenGeneralSettings: () => { settingsOpens += 1; },
  });
  try {
    await react.act(async () => liveSocket.reply(starterRequests().at(-1).id, dynamicSlateResponse('sess-topic-switch', 'ON')));
    await react.act(settle);
    assert.equal(visibleSuggestionTitles().length, 4);
    await react.act(async () => liveSocket.onmessage({ data: JSON.stringify({
      type: 'event', event: 'topic_starters.control_changed', payload: { enabled: false },
    }) }));
    assert.equal(visibleSuggestionTitles().length, 4, 'preset/cached cards stay while recommendation is off');
    assert.equal(document.querySelector('[data-testid="chat-panel-topic-disabled-hint"]'), null);
    await react.act(async () => document.querySelector('[data-testid="chat-panel-welcome-suggestion"]').dispatchEvent(
      new window.MouseEvent('mouseover', { bubbles: true }),
    ));
    assert.ok(document.querySelector('[data-testid="chat-panel-topic-disabled-hint"]'));
    await react.act(async () => document.querySelector('[data-testid="chat-panel-topic-general-settings"]').click());
    assert.equal(settingsOpens, 1);
    await react.act(settle);
    await react.act(async () => liveSocket.reply(
      starterRequests().at(-1).id,
      { ...dynamicSlateResponse('sess-topic-switch', 'PRESET'), preparation_state: 'disabled' },
    ));
    await react.act(settle);
    assert.equal(visibleSuggestionTitles().length, 4);
    assert.ok(document.querySelector('[data-testid="chat-panel-topic-disabled-hint"]'));
    const disabledHint = document.querySelector('[data-testid="chat-panel-topic-disabled-hint"]');
    assert.ok(disabledHint.textContent.includes('Settings - General'));
    assert.ok(document.querySelector('[data-testid="chat-panel-welcome-banner-bubble"]').contains(disabledHint));
    assert.equal(document.querySelector('[data-testid="chat-panel-topic-general-settings"]').className, 'chat-welcome__campaign-claim');
    await react.act(async () => liveSocket.onmessage({ data: JSON.stringify({
      type: 'event', event: 'topic_starters.control_changed', payload: { enabled: true },
    }) }));
    await react.act(settle);
    assert.equal(document.querySelector('[data-testid="chat-panel-topic-disabled-hint"]'), null, 'enabled must not show the disabled hint');
    await react.act(async () => liveSocket.reply(starterRequests().at(-1).id, dynamicSlateResponse('sess-topic-switch', 'BACK')));
    await react.act(settle);
    assert.equal(visibleSuggestionTitles().length, 4);
    assert.equal(document.querySelector('[data-testid="chat-panel-topic-disabled-hint"]'), null);
  } finally {
    await panel.unmount();
  }
});

test('enabled recommendations show a shared unavailable hint without settings CTA', async () => {
  for (const state of ['missing_configuration', 'provider_unavailable']) {
    const panel = await mountWelcome(`sess-enabled-${state}`, {
      holdResponses: true, onOpenGeneralSettings: () => { throw new Error('settings CTA removed'); },
    });
    try {
      await react.act(async () => liveSocket.reply(starterRequests().at(-1).id, {
        available: false, preparation_state: state, reason: state,
      }));
      await react.act(settle);
      assert.equal(document.querySelector('[data-testid="chat-panel-topic-disabled-hint"]'), null);
      const status = document.querySelector('[role="status"]');
      assert.ok(status);
      assert.ok(status.textContent.includes('unavailable'));
      assert.ok(status.classList.contains('chat-suggestions__title'));
      assert.equal(document.querySelector('[data-testid="chat-panel-topic-settings"]'), null);
      assert.equal(status.textContent.includes('Check settings'), false);
      assert.equal(document.querySelector('[data-testid="chat-panel-topic-general-settings"]'), null);
      assert.equal(document.body.textContent.includes('For topics that understand you better'), false);
    } finally {
      await panel.unmount();
    }
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
      work_mode: 'work',
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

test('topic recommendations show no dismiss button', async () => {
  const panel = await mountWelcome('sess-without-dismiss', { holdResponses: true });
  try {
    await react.act(async () => liveSocket.reply(starterRequests()[0].id,
      dynamicSlateResponse('sess-without-dismiss', 'TOPIC', 4)));
    await react.act(settle);
    assert.equal(visibleSuggestionTitles().length, 4);
    assert.equal(document.querySelector('[data-testid="chat-panel-topic-dismiss"]'), null);
    assert.equal(document.querySelectorAll('.chat-suggestion-card button').length, 4);
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
const recoveredPromptSuggestions = new Map();

async function mountConversation({ onSendMessage, completeInitial = true } = {}) {
  recoveredPromptSuggestions.clear();
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

test('next question appears only in the composer without a label; Tab fills without sending', async () => {
  const panel = await mountConversation();
  try {
    const id = await pushSuggestion();
    assert.equal(box(), null);
    assert.equal(editor().getAttribute('data-placeholder').startsWith(SUGGESTION), true);
    assert.ok(editor().getAttribute('data-placeholder').includes(SUGGESTION));
    assert.equal(useChatStore.getState().runtimes[SESSION].inputValue, '');
    assert.equal(await pressInComposer('Tab'), true);
    assert.equal(useChatStore.getState().runtimes[SESSION].inputValue, SUGGESTION);
    assert.deepEqual(panel.sends, []);
    await send();
    assert.deepEqual(panel.sends, [
      { content: SUGGESTION, provenance: { ...ACCEPTED_ORIGIN, prompt_suggestion: { suggestion_id: id, edited: false } } },
    ]);
  } finally {
    await panel.unmount();
  }
});

test('returning to a conversation recovers a missed suggestion in the composer', async () => {
  const panel = await mountConversation();
  try {
    recoveredPromptSuggestions.set(SESSION, [{ session_id: SESSION,
      request_id: completedRequests.get(SESSION), suggestion_id: 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
      text: 'Recover the next question from the backend' }]);
    await panel.navigate(OTHER_SESSION);
    await panel.navigate(SESSION);
    assert.ok(editor().getAttribute('data-placeholder').includes('Recover the next question from the backend'));
    assert.equal(await pressInComposer('Tab'), true);
    assert.equal(useChatStore.getState().runtimes[SESSION].inputValue, 'Recover the next question from the backend');
  } finally {
    await panel.unmount();
  }
});

test('a pushed suggestion refreshes a stale disabled switch before displaying', async () => {
  const panel = await mountConversation();
  try {
    await panel.setPageActive(false);
    await clickTestId('settings-prompt-suggestion-enabled');
    recoveredPromptSuggestions.set(SESSION, [{ session_id: SESSION,
      request_id: completedRequests.get(SESSION), suggestion_id: 'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      text: 'The server enabled suggestions in another connection' }]);
    await pushSuggestion({ text: 'The server enabled suggestions in another connection' });
    await panel.setPageActive(true);
    assert.ok(editor().getAttribute('data-placeholder').includes('The server enabled suggestions in another connection'));
  } finally {
    await panel.unmount();
  }
});

test('a new completed answer replaces the composer next-question suggestion', async () => {
  const panel = await mountConversation();
  try {
    await pushSuggestion();
    await startRequest(SESSION);
    assert.equal(editor().getAttribute('data-placeholder').includes(SUGGESTION), false);
    await answer();
    await pushSuggestion({ text: 'Compare the options in a table' });
    assert.ok(editor().getAttribute('data-placeholder').includes('Compare the options in a table'));
    await setComposer(SESSION, 'My own question');
    assert.equal(editor().getAttribute('data-placeholder').includes('Compare the options in a table'), false);
  } finally {
    await panel.unmount();
  }
});

test('clearing a Tab-filled suggestion restores the original placeholder after browser line-break remnants', async () => {
  const panel = await mountConversation();
  try {
    const originalPlaceholder = editor().getAttribute('data-placeholder');
    await pushSuggestion();
    assert.equal(await pressInComposer('Tab'), true);
    assert.equal(useChatStore.getState().runtimes[SESSION].inputValue, SUGGESTION);
    for (const remnants of ['<br>', '<div><br></div>']) {
      await react.act(async () => {
        editor().innerHTML = remnants;
        editor().dispatchEvent(new dom.window.InputEvent('input', { bubbles: true, inputType: 'deleteContentBackward' }));
        await settle();
      });
      assert.equal(editor().childNodes.length, 0, 'the editor must match :empty for its placeholder');
      assert.equal(editor().getAttribute('data-placeholder'), originalPlaceholder);
      assert.equal(useChatStore.getState().runtimes[SESSION].inputValue, '');
    }
    assert.equal(await pressInComposer('Tab'), false, 'clearing must not restore an already accepted suggestion');
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


test('only the first suggestion is shown in the composer and no reply chip is rendered', async () => {
  const panel = await mountConversation();
  try {
    const first = await pushSuggestion({ text: 'Compare the options' });
    await pushSuggestion({ text: 'Explain the costs' });
    assert.equal(box(), null);
    assert.equal(document.querySelector('[data-testid="chat-panel-prompt-suggestion-chip-send"]'), null);
    assert.ok(editor().getAttribute('data-placeholder').startsWith('Compare the options'));
    assert.equal(await pressInComposer('Tab'), true);
    assert.deepEqual(outcomes(), [['shown', first, null], ['accepted', first, 'tab']]);
  } finally {
    await panel.unmount();
  }
});

for (const args of cases) registerTest(...args);
