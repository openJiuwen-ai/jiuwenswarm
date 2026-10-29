import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { act, createElement, Fragment } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';
import { createServer } from 'vite';
import svgr from 'vite-plugin-svgr';

// Use the production transform for the provider catalog's import.meta.glob and assets.
const vite = await createServer({
  configFile: false,
  plugins: [svgr()],
  cacheDir: 'node_modules/.cache/model-picker/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
  define: { 'import.meta.env.DEV': false },
});
let modules;
try {
  modules = await Promise.all([
    vite.ssrLoadModule('/src/components/ModelPicker/index.tsx'),
    vite.ssrLoadModule('/src/components/ChatPanel/ChatModelSelector.tsx'),
    vite.ssrLoadModule('/src/components/CronPanel/CronTaskDrawer.tsx'),
    vite.ssrLoadModule('/src/stores/sessionStore.ts'),
    vite.ssrLoadModule('/src/stores/authStore.ts'),
    vite.ssrLoadModule('/src/stores/chatStore.ts'),
    vite.ssrLoadModule('/src/components/CronPanel/index.tsx'),
    vite.ssrLoadModule('/src/services/webClient.ts'),
    vite.ssrLoadModule('/src/multi-session/state/conversationModelSelection.ts'),
    vite.ssrLoadModule('/src/multi-session/state/newConversationLifecycle.ts'),
    vite.ssrLoadModule('/src/features/settings/modules/models/modelGroupAdapters.ts'),
    vite.ssrLoadModule('/src/features/multiModelCollaboration/featureConfig.ts'),
    vite.ssrLoadModule('/src/features/settings/modules/models/ModelGroupDialog.tsx'),
    vite.ssrLoadModule('/src/features/settings/services/SettingsServicesProvider'),
    vite.ssrLoadModule('/src/stores/teamSelectorStore.ts'),
    vite.ssrLoadModule('/src/features/teamOrganization/featureConfig.ts'),
  ]);
} finally {
  await vite.close();
}
const [
  { default: ModelPicker },
  { default: ChatModelSelector },
  { default: CronTaskDrawer },
  { useSessionStore },
  { useAuthStore },
  { useChatStore },
  { default: CronPanel },
  { webClient },
  { buildConversationModelPayload, isConversationModelGroupBlocked },
  { registerCreatedConversation, resetNewConversationRuntime, forgetCreatedConversation },
  {
    buildModelGroupUpsertPayload,
    findModelGroupReferencesForModel,
    formatModelSelectionReferenceScopes,
    extractModelSelectionReferences,
  },
  { setMultiModelCollaborationEnabled, isMultiModelCollaborationEnabled },
  { ModelGroupDialog },
  { SettingsServicesProvider },
  { useTeamSelectorStore },
  { setTeamOrganizationUiEnabled, isTeamOrganizationUiEnabled },
] = modules;

const resources = Object.fromEntries(
  ['en', 'zh'].map((language) => [
    language,
    { translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')) },
  ]),
);
const catalog = [
  { model_name: 'free-model', alias: 'Free Alias', is_free: true },
  { model_name: 'configured-a', alias: 'Configured A', is_default: true },
  { model_name: 'configured-a', alias: 'Secondary connection', is_default: false },
  { model_name: 'configured-b', alias: 'Configured B', is_free: false },
];
const sessionId = 'shared-model-picker-test';
const initialForm = {
  name: 'Existing scheduled task',
  description: 'Draft description',
  modelName: 'configured-a',
  mode: 'agent',
  cronExpr: '0 0 2 * * ? *',
  timezone: 'Asia/Shanghai',
  targets: 'web',
  wakeOffsetSeconds: 0,
  projectDir: null,
  projectId: null,
  workMode: null,
  effectiveDate: null,
  enabled: true,
};

async function withFixture(run, language = 'en') {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost/',
  });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  if (!dom.window.ResizeObserver) {
    dom.window.ResizeObserver = class {
      observe() {}
      unobserve() {}
      disconnect() {}
    };
  }
  const previousGlobals = new Map();
  for (const [name, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    // 组件里会 new CustomEvent（如 requestLogin 派发的 jiuwen:auth-required），
    // 不挂到全局的话那句直接 ReferenceError
    CustomEvent: dom.window.CustomEvent,
    ResizeObserver: class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
    MutationObserver: dom.window.MutationObserver,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previousGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  const previousSession = useSessionStore.getState();
  const previousChat = useChatStore.getState();
  const previousAuth = useAuthStore.getState();
  const previousCollaboration = isMultiModelCollaborationEnabled();
  setMultiModelCollaborationEnabled(false);
  // 预置成「状态已查过、活动没开」：否则组件挂载时会真的去查登录状态，测试环境里
  // 没有后端，查询失败会起一条最长 60 秒的退避重试，整个测试文件被拖到一分半钟。
  // 需要活动开着的用例在 run 里自己改。
  useAuthStore.setState({ initialized: true, enabled: false, islogin: false, campaignState: 'off' });
  const i18n = i18next.createInstance();
  await i18n.init({ lng: language, resources, initImmediate: false, showSupportNotice: false });
  const root = createRoot(document.getElementById('root'));
  const byId = (id) => document.querySelector(`[data-testid="${id}"]`);
  const mount = async (...children) =>
    act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(Fragment, null, ...children)));
    });
  const click = async (element) => {
    assert.ok(element, 'click target exists');
    await act(async () => {
      element.dispatchEvent(new dom.window.MouseEvent('pointerdown', { bubbles: true }));
      element.click();
    });
  };
  try {
    useSessionStore.getState().setAvailableModels(catalog, 'configured-a');
    useSessionStore.getState().ensureRuntime(sessionId);
    useSessionStore.getState().setMode(sessionId, 'agent');
    useSessionStore.getState().setSelectedModelName(sessionId, 'configured-a');
    useChatStore.setState({ activeSessionId: sessionId });
    await run({ dom, mount, click, byId });
  } finally {
    await act(async () => root.unmount());
    useSessionStore.setState(previousSession, true);
    useChatStore.setState(previousChat, true);
    useAuthStore.setState(previousAuth, true);
    setMultiModelCollaborationEnabled(previousCollaboration);
    for (const [name, descriptor] of previousGlobals) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    dom.window.close();
  }
}

function cronDrawer(overrides = {}) {
  return createElement(CronTaskDrawer, {
    mode: 'edit',
    initial: { ...initialForm },
    projects: [],
    targetOptions: [{ value: 'web', label: 'Web' }],
    onClose() {},
    onSubmit() {},
    ...overrides,
  });
}

test('Team conversations keep model-group selection enabled', () => {
  const selectorSource = readFileSync(
    new URL('../src/components/ChatPanel/ChatModelSelector.tsx', import.meta.url),
    'utf8',
  );
  assert.doesNotMatch(selectorSource, /mode\s*!==\s*['"]team['"]/);
  assert.doesNotMatch(selectorSource, /next\.type\s*===\s*['"]model_group['"]\)\s*return/);
  assert.match(selectorSource, /includeModelGroups/);
  assert.match(selectorSource, /model_selection:\s*next/);
});

test('chat and scheduled tasks share the ordinary model catalog', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    await mount(createElement(ChatModelSelector), cronDrawer());
    for (const prefix of ['chat-panel-model-selector', 'cron-model-picker']) {
      await click(byId(`${prefix}-trigger`));
      const menu = byId(`${prefix}-menu`);
      assert.deepEqual(
        [...menu.querySelectorAll('.model-select__section-header')].map((node) => node.textContent),
        ['Limited-time Free Models', 'Configured Models'],
      );
      assert.deepEqual(
        [...menu.querySelectorAll('[role="menuitemradio"]')].map((node) => node.textContent),
        ['Free Alias', 'Configured A', 'Secondary connection', 'Configured B'],
      );
      assert.equal(menu.querySelector('[aria-checked="true"]').dataset.variant, 'configured-a');
      await click(document.body);
    }
  });
});

test('限时免费模型：没登录拿到之前，这一栏只放一个「获取」入口', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    // 活动在跑但还没登录：免费模型一个都没有
    useAuthStore.setState({ enabled: true, initialized: true, islogin: false, campaignState: 'active' });
    useSessionStore.getState().setAvailableModels(
      catalog.filter((model) => model.is_free !== true),
      'configured-a',
    );
    await mount(createElement(ChatModelSelector));
    await click(byId('chat-panel-model-selector-trigger'));
    const menu = byId('chat-panel-model-selector-menu');

    // 分组标题还在——用户得先知道有这回事，再点进去拿
    assert.deepEqual(
      [...menu.querySelectorAll('.model-select__section-header')].map((node) => node.textContent),
      ['Limited-time Free Models', 'Configured Models'],
    );
    const cta = byId('chat-panel-model-selector-free-cta');
    assert.ok(cta, '未登录时应出现「获取限时免费模型」入口');
    // 它不是可选项：选中态和模型条目不能混在一起
    assert.equal(cta.getAttribute('role'), null);
    assert.deepEqual(
      [...menu.querySelectorAll('[role="menuitemradio"]')].map((node) => node.textContent),
      ['Configured A', 'Secondary connection', 'Configured B'],
    );

    // 点它派发登录事件（LoginDialog 监听），并收起下拉
    let requested = 0;
    const onRequest = () => { requested += 1; };
    window.addEventListener('jiuwen:auth-required', onRequest);
    await click(cta);
    window.removeEventListener('jiuwen:auth-required', onRequest);
    assert.equal(requested, 1);
    assert.equal(byId('chat-panel-model-selector-menu'), null);
  });
});

test('限时免费模型：活动没在跑时，这一栏整个不出现', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    useAuthStore.setState({ enabled: false, initialized: true, islogin: false });
    useSessionStore.getState().setAvailableModels(
      catalog.filter((model) => model.is_free !== true),
      'configured-a',
    );
    await mount(createElement(ChatModelSelector));
    await click(byId('chat-panel-model-selector-trigger'));
    const menu = byId('chat-panel-model-selector-menu');
    assert.deepEqual(
      [...menu.querySelectorAll('.model-select__section-header')].map((node) => node.textContent),
      ['Configured Models'],
    );
    assert.equal(byId('chat-panel-model-selector-free-cta'), null);
  });
});

test('selecting a scheduled-task model submits its ID and leaves the active chat unchanged', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    let submitted;
    await mount(
      createElement(ChatModelSelector),
      cronDrawer({
        onSubmit(value) {
          submitted = value;
        },
      }),
    );
    await click(byId('cron-model-picker-trigger'));
    await click(byId('cron-model-picker-menu').querySelector('[data-variant="configured-b"]'));
    assert.equal(byId('cron-model-picker-menu'), null);
    assert.equal(byId('cron-model-picker-trigger').textContent, 'Configured B');
    assert.equal(byId('chat-panel-model-selector-trigger').textContent, 'Configured A');
    assert.equal(useSessionStore.getState().getEffectiveModelName(sessionId), 'configured-a');
    await click(byId('cron-drawer-submit-btn'));
    assert.deepEqual(submitted, { ...initialForm, modelName: 'configured-b' });
  });
});

test('selecting a chat model preserves the scheduled-task draft and canonical request model', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    let submitted;
    await mount(
      createElement(ChatModelSelector),
      cronDrawer({
        onSubmit(value) {
          submitted = value;
        },
      }),
    );
    await click(byId('chat-panel-model-selector-trigger'));
    await click(byId('chat-panel-model-selector-menu').querySelector('[data-variant="configured-b"]'));
    assert.equal(useSessionStore.getState().getEffectiveModelName(sessionId), 'configured-b');
    assert.equal(byId('chat-panel-model-selector-trigger').textContent, 'Configured B');
    assert.equal(byId('cron-model-picker-trigger').textContent, 'Configured A');
    await click(byId('cron-drawer-submit-btn'));
    assert.deepEqual(submitted, initialForm);
  });
});

test('a historical chat alias still resolves to the selected model after extraction', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    useSessionStore.getState().setSelectedModelName(sessionId, 'Configured B');
    await mount(createElement(ChatModelSelector));
    assert.equal(byId('chat-panel-model-selector-trigger').textContent, 'Configured B');
    await click(byId('chat-panel-model-selector-trigger'));
    assert.equal(document.querySelector('[aria-checked="true"]').dataset.variant, 'configured-b');
    assert.equal(useSessionStore.getState().getEffectiveModelName(sessionId), 'configured-b');
  });
});

test('opening a stored team task from the list preserves its mode and model', async (t) => {
  const job = {
    id: 'stored-team-task',
    name: 'Stored team task',
    description: 'Keep its model and execution mode when editing',
    model_name: 'configured-a',
    mode: 'team.work.normal',
    cron_expr: '0 0 2 * * ? *',
    timezone: 'Asia/Shanghai',
    targets: 'web',
    wake_offset_seconds: 0,
    enabled: false,
    expired: false,
    project_id: 'default',
  };
  t.mock.method(webClient, 'request', async (method) => {
    switch (method) {
      case 'cron.job.list':
        return { jobs: [job] };
      case 'project.list':
        return { projects: [] };
      case 'channel.get':
        return { channels: [{ channel_id: 'web' }] };
      case 'channel.xiaoyi.get_conf':
        return { config: {} };
      default:
        throw new Error(`Unexpected request: ${method}`);
    }
  });
  await withFixture(async ({ mount, click, byId }) => {
    await mount(createElement(CronPanel, { sessionId, onCreateViaChat() {}, onSelectSession() {} }));
    await click(byId('cron-job-edit-btn'));
    assert.equal(byId('cron-mode-trigger').dataset.variant, 'team');
    assert.equal(byId('cron-model-picker-trigger').textContent, 'Configured A');
    await click(byId('cron-model-picker-trigger'));
    await click(byId('cron-model-picker-menu').querySelector('[data-variant="configured-b"]'));
    assert.equal(byId('cron-mode-trigger').dataset.variant, 'team');
    assert.equal(byId('cron-model-picker-trigger').textContent, 'Configured B');
  });
});

test('an unavailable saved task model stays visible and is not replaced during catalog refresh', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    let submitted;
    await mount(
      cronDrawer({
        initial: { ...initialForm, modelName: 'removed-model' },
        onSubmit(value) {
          submitted = value;
        },
      }),
    );
    await act(async () => useSessionStore.getState().setAvailableModels(catalog, 'configured-b'));
    assert.equal(byId('cron-model-picker-trigger').textContent, 'removed-model');
    await click(byId('cron-model-picker-trigger'));
    assert.equal(document.querySelector('[aria-checked="true"]'), null);
    await click(document.body);
    await click(byId('cron-drawer-submit-btn'));
    assert.equal(submitted.modelName, 'removed-model');
  });
});

test('a new task keeps an unselected model instead of borrowing the active chat default', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    let submitted;
    await mount(
      cronDrawer({
        mode: 'create',
        initial: { ...initialForm, modelName: null },
        onSubmit(value) {
          submitted = value;
        },
      }),
    );
    assert.equal(byId('cron-model-picker-trigger').textContent, 'Select Model');
    await click(byId('cron-drawer-submit-btn'));
    assert.equal(submitted.modelName, null);
  });
});

test('locked task models cannot be changed; disabling an open picker closes it', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    await mount(cronDrawer({ proactiveLocked: true }));
    assert.equal(byId('cron-model-picker-trigger').disabled, true);
    await click(byId('cron-model-picker-trigger'));
    assert.equal(byId('cron-model-picker-menu'), null);
    const picker = (disabled) => createElement(ModelPicker, { value: null, onChange() {}, disabled });
    await mount(picker(false));
    await click(byId('model-picker-trigger'));
    assert.ok(byId('model-picker-menu'));
    await mount(picker(true));
    assert.equal(byId('model-picker-menu'), null);
    await mount(picker(false));
    assert.equal(byId('model-picker-menu'), null);
  });
});

for (const language of ['zh', 'en']) {
  test(`empty catalogs and single model groups use the shared ${language} translations`, async () => {
    await withFixture(async ({ mount, click, byId }) => {
      useSessionStore.getState().setAvailableModels([]);
      await mount(createElement(ModelPicker, { value: null, onChange() {} }));
      await click(byId('model-picker-trigger'));
      assert.equal(byId('model-picker-empty').textContent, resources[language].translation.chat.modelSelector.empty);
      assert.equal(document.querySelectorAll('.model-select__section-header').length, 0);
      assert.deepEqual(useSessionStore.getState().availableModels, []);
      assert.equal(useSessionStore.getState().defaultModelName, null);
      for (const model of [catalog[0], catalog[1]]) {
        await act(async () => useSessionStore.getState().setAvailableModels([model]));
        const headings = [...document.querySelectorAll('.model-select__section-header')];
        assert.equal(headings.length, 1);
        assert.equal(
          headings[0].textContent,
          resources[language].translation.chat.modelSelector[model.is_free ? 'free' : 'configured'],
        );
        assert.equal(byId('model-picker-empty'), null);
      }
    }, language);
  });
}

test('Escape and outside clicks dismiss the portal without changing the controlled value', async () => {
  await withFixture(async ({ dom, mount, click, byId }) => {
    const changes = [];
    await mount(createElement(ModelPicker, { value: 'configured-a', onChange: (value) => changes.push(value) }));
    await click(byId('model-picker-trigger'));
    await act(async () =>
      byId('model-picker-trigger').dispatchEvent(
        new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
      ),
    );
    assert.equal(byId('model-picker-menu'), null);
    assert.equal(document.activeElement, byId('model-picker-trigger'));
    await click(byId('model-picker-trigger'));
    await click(document.body);
    assert.equal(byId('model-picker-menu'), null);
    assert.deepEqual(changes, []);
  });
});

const stableCatalog = [
  { model_id: 'mdl-a', model_name: 'same-model', alias: 'Primary connection', model_provider: 'OpenAI', is_default: true },
  { model_id: 'mdl-b', model_name: 'same-model', alias: 'Secondary connection', model_provider: 'OpenAI', is_default: false },
];
const stableGroup = {
  model_group_id: 'mgp-stable', display_name: 'Stable model group', enabled: true,
  routes: [
    { route_id: 'rte-a', model_id: 'mdl-a', request_overrides: { temperature: 0.2 }, timeout: 25 },
    { route_id: 'rte-b', model_id: 'mdl-b', request_overrides: { temperature: 0.8 }, timeout: 35 },
  ], request_config: { temperature: 0.5 },
};
function setupStableCatalog() {
  const state = useSessionStore.getState();
  state.setAvailableModels(stableCatalog, 'same-model');
  state.setAvailableModelGroups([stableGroup]);
  state.setSelectedModelSelection(sessionId, { type: 'model', id: 'mdl-a' });
}

for (const mode of ['agent', 'agent.work.normal', 'agent.code.normal', 'agent.work.plan', 'agent.code.plan']) {
  test(`${mode} exposes shared model groups only when collaboration is enabled`, async (t) => {
    const persisted = [];
    t.mock.method(webClient, 'request', async (method, params) => {
      assert.equal(method, 'session.selection.set');
      persisted.push(params);
      return { success: true };
    });
    await withFixture(async ({ mount, click, byId }) => {
      setupStableCatalog();
      useSessionStore.getState().setMode(sessionId, mode);
      await mount(createElement(ChatModelSelector));
      await click(byId('chat-panel-model-selector-trigger'));
      assert.equal(byId('chat-panel-model-selector-group-option'), null);
      assert.equal(byId('chat-panel-model-selector-moa-option'), null);
      await click(document.body);
      await act(async () => setMultiModelCollaborationEnabled(true));
      await click(byId('chat-panel-model-selector-trigger'));
      await click(byId('chat-panel-model-selector-group-option'));
      assert.equal(byId('chat-panel-model-selector-label').textContent, 'Stable model group');
      assert.deepEqual(useSessionStore.getState().getOutgoingModelParams(sessionId), {
        model_selection: { type: 'model_group', id: 'mgp-stable' },
      });
      assert.deepEqual(persisted, [{ session_id: sessionId, model_selection: { type: 'model_group', id: 'mgp-stable' } }]);
      await click(byId('chat-panel-model-selector-trigger'));
      await click(byId('chat-panel-model-selector-option'));
      assert.deepEqual(useSessionStore.getState().getOutgoingModelParams(sessionId), {
        model_selection: { type: 'model', id: 'mdl-a' },
      });
      assert.equal(isMultiModelCollaborationEnabled(), true);
    });
  });
}

for (const language of ['zh', 'en']) {
  test(`${language}: disabling collaboration preserves group and picker label without an extra warning`, async () => {
    await withFixture(async ({ mount, click, byId }) => {
      setupStableCatalog();
      useSessionStore.getState().setSelectedModelSelection(sessionId, { type: 'model_group', id: 'mgp-stable' });
      setMultiModelCollaborationEnabled(true);
      await mount(createElement(ChatModelSelector));
      await act(async () => setMultiModelCollaborationEnabled(false));
      assert.equal(
        byId('chat-panel-model-selector-label').textContent,
        language === 'zh' ? '请选择普通模型' : 'Select an individual model',
      );
      assert.equal(byId('chat-panel-model-selector-group-disabled'), null);
      assert.equal(document.querySelector('[role="status"]'), null);
      assert.doesNotMatch(document.body.textContent, /多模型协同已关闭|Multi-model collaboration is off/);
      await click(byId('chat-panel-model-selector-trigger'));
      assert.equal(byId('chat-panel-model-selector-group-option'), null);
      assert.equal(document.querySelector('[aria-checked="true"]'), null, 'no fallback model is selected');
      assert.deepEqual(useSessionStore.getState().getRuntime(sessionId).selectedModelSelection, {
        type: 'model_group',
        id: 'mgp-stable',
      });
      await act(async () => setMultiModelCollaborationEnabled(true));
      assert.equal(byId('chat-panel-model-selector-label').textContent, 'Stable model group');
    }, language);
  });
}

for (const source of ['current', 'list']) {
  test(`scheduled ${source} metadata and team selections retain upstream groups with the feature off`, async () => {
    await withFixture(async ({ mount, click, byId }) => {
      setupStableCatalog();
      const session = { session_id: sessionId, title: 'Scheduled', mode: 'agent', cron_id: 'job',
        status: 'active', message_count: 1, created_at: '2026-10-14T00:00:00Z', updated_at: '2026-10-14T00:00:00Z' };
      if (source === 'current') useSessionStore.getState().setCurrentSession(session);
      else useSessionStore.getState().setSessions([session]);
      await mount(createElement(ChatModelSelector));
      await click(byId('chat-panel-model-selector-trigger'));
      assert.ok(byId('chat-panel-model-selector-group-option'));
      assert.equal(byId('chat-panel-model-selector-moa-option'), null);
      await click(document.body);
      await act(async () => useSessionStore.getState().setCurrentSession({ ...session, mode: 'team', cron_id: null }));
      await mount(createElement(ChatModelSelector));
      await click(byId('chat-panel-model-selector-trigger'));
      assert.ok(byId('chat-panel-model-selector-group-option'));
    });
  });
}

test('a running task locks the picker', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    setupStableCatalog();
    setMultiModelCollaborationEnabled(true);
    await mount(createElement(ChatModelSelector, { disabled: true }));
    assert.equal(byId('chat-panel-model-selector-trigger').disabled, true);
    await click(byId('chat-panel-model-selector-trigger'));
    assert.equal(byId('chat-panel-model-selector-menu'), null);
  });
});

test('collaboration menu supports keyboard navigation and Escape restores focus', async () => {
  await withFixture(async ({ dom, mount, click, byId }) => {
    setupStableCatalog();
    setMultiModelCollaborationEnabled(true);
    await mount(createElement(ChatModelSelector));
    await click(byId('chat-panel-model-selector-trigger'));
    const group = byId('chat-panel-model-selector-group-option');
    group.focus();
    await act(async () => group.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'End', bubbles: true })));
    assert.ok(document.activeElement.matches('button'));
    await act(async () => document.activeElement.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Home', bubbles: true })));
    assert.equal(document.activeElement.dataset.variant, 'mdl-a');
    await act(async () => group.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true })));
    assert.equal(byId('chat-panel-model-selector-menu'), null);
    assert.equal(document.activeElement, byId('chat-panel-model-selector-trigger'));
  });
});

for (const mode of ['agent', 'team']) {
  test(`${mode}: no model groups leaves the ordinary model picker without an extra prompt`, async () => {
    await withFixture(async ({ mount, click, byId }) => {
      useSessionStore.getState().setAvailableModelGroups([]);
      useSessionStore.getState().setMode(sessionId, mode);
      setMultiModelCollaborationEnabled(true);
      await mount(createElement(ChatModelSelector));
      assert.equal(byId('chat-panel-model-selector-create-group'), null);
      assert.equal(byId('chat-panel-model-selector-trigger').textContent, 'Configured A');
      await click(byId('chat-panel-model-selector-trigger'));
      assert.ok(byId('chat-panel-model-selector-menu').querySelector('[data-variant="configured-b"]'));
      assert.equal(byId('chat-panel-model-selector-group-option'), null);
    });
  });
}

test('stable choices survive draft, creation, metadata restore and fork without enhancement state', async () => {
  await withFixture(async () => {
    setupStableCatalog();
    const selection = { type: 'model_group', id: 'mgp-stable' };
    resetNewConversationRuntime({ mode: 'agent', selectedModelName: null, selectedModelSelection: selection });
    assert.deepEqual(useSessionStore.getState().getOutgoingModelParams('new'), { model_selection: selection });
    const created = registerCreatedConversation('group-created', { mode: 'agent', selectedModelName: null, selectedModelSelection: selection });
    useSessionStore.getState().removeRuntime(created.session_id);
    useSessionStore.getState().setCurrentSession(created);
    assert.deepEqual(useSessionStore.getState().getRuntime(created.session_id).selectedModelSelection, selection);
    useSessionStore.getState().setCurrentSession({ ...created, session_id: 'group-fork' });
    useSessionStore.getState().setSelectedModelSelection('group-fork', { type: 'model', id: 'mdl-b' });
    assert.deepEqual(useSessionStore.getState().getOutgoingModelParams('group-fork'), { model_selection: { type: 'model', id: 'mdl-b' } });
    assert.deepEqual(useSessionStore.getState().getOutgoingModelParams(created.session_id), { model_selection: selection });
    assert.equal(isMultiModelCollaborationEnabled(), false);
    assert.equal('moaEnabled' in useSessionStore.getState().getRuntime(created.session_id), false);
    forgetCreatedConversation(created.session_id);
  });
});

test('legacy moa metadata never enables collaboration or selects a group', async () => {
  await withFixture(async () => {
    const created = registerCreatedConversation('legacy-moa', { mode: 'agent', selectedModelName: 'configured-b' });
    useSessionStore.getState().setCurrentSession({ ...created, moa_enabled: true });
    assert.equal(isMultiModelCollaborationEnabled(), false);
    assert.equal(useSessionStore.getState().getRuntime(created.session_id).selectedModelSelection?.type === 'model_group', false);
    assert.equal('moaEnabled' in useSessionStore.getState().getRuntime(created.session_id), false);
    forgetCreatedConversation(created.session_id);
  });
});

test('stable model payloads contain no independent MoA state', () => {
  for (const mode of ['agent', 'agent.work.normal', 'agent.code.normal', 'team.code.normal']) {
    assert.deepEqual(buildConversationModelPayload({ mode, selectedModelName: 'ordinary-model',
      selectedModelSelection: { type: 'model_group', id: 'mgp-stable' } }), { model_selection: { type: 'model_group', id: 'mgp-stable' } });
    assert.deepEqual(buildConversationModelPayload({ mode, selectedModelName: 'ordinary-model' }), { model_name: 'ordinary-model' });
  }
  assert.equal(isConversationModelGroupBlocked({ mode: 'agent', selectedModelSelection: { type: 'model_group', id: 'g' }, collaborationEnabled: false }), true);
  assert.equal(isConversationModelGroupBlocked({ mode: 'agent', cronId: 'job', selectedModelSelection: { type: 'model_group', id: 'g' }, collaborationEnabled: false }), false);
});

test('upstream group edit retains stable route identities and order', () => {
  const edited = buildModelGroupUpsertPayload({ group: stableGroup, displayName: 'Renamed group', routes: [...stableGroup.routes].reverse() });
  assert.equal(edited.model_group_id, 'mgp-stable');
  assert.deepEqual(edited.routes.map(route => [route.route_id, route.model_id]), [['rte-b', 'mdl-b'], ['rte-a', 'mdl-a']]);
});


test('catalog refresh preserves an unavailable model group in the unsent draft', async () => {
  await withFixture(async () => {
    setupStableCatalog();
    resetNewConversationRuntime({ mode: 'agent', selectedModelName: null,
      selectedModelSelection: { type: 'model_group', id: 'mgp-stable' } });
    useSessionStore.getState().setAvailableModelGroups([]);
    useSessionStore.getState().setAvailableModels(stableCatalog, 'same-model');
    assert.deepEqual(useSessionStore.getState().getRuntime('new').selectedModelSelection,
      { type: 'model_group', id: 'mgp-stable' });
    assert.equal(useSessionStore.getState().getRuntime('new').selectedModelName, null);
  });
});


test('same-name connections display and send the selected stable model identity', async () => {
  await withFixture(async ({ mount, byId }) => {
    setupStableCatalog();
    useSessionStore.getState().setSelectedModelSelection(sessionId, { type: 'model', id: 'mdl-b' });
    await mount(createElement(ChatModelSelector));
    assert.equal(byId('chat-panel-model-selector-label').textContent, 'Secondary connection');
    assert.deepEqual(useSessionStore.getState().getOutgoingModelParams(sessionId), { model_selection: { type: 'model', id: 'mdl-b' } });
  });
});

for (const aliases of [true, false]) {
  test(`same-name non-default connections remain selectable (${aliases ? 'with aliases' : 'without aliases'})`, async (t) => {
    const persisted = [];
    t.mock.method(webClient, 'request', async (method, params) => {
      assert.equal(method, 'session.selection.set');
      persisted.push(params);
      return { success: true };
    });
    await withFixture(async ({ mount, click, byId }) => {
      setupStableCatalog();
      const models = stableCatalog.map((model) => ({ ...model, alias: aliases ? model.alias : '' }));
      useSessionStore.getState().setAvailableModels(models, 'same-model');
      await mount(createElement(ChatModelSelector));
      await click(byId('chat-panel-model-selector-trigger'));
      const option = byId('chat-panel-model-selector-menu').querySelector('[data-variant="mdl-b"]');
      assert.ok(option, 'the non-default connection is present');
      const label = aliases ? 'Secondary connection' : 'same-model';
      assert.equal(option.textContent, label);
      await click(option);
      assert.equal(byId('chat-panel-model-selector-label').textContent, label);
      assert.deepEqual(useSessionStore.getState().getOutgoingModelParams(sessionId), {
        model_selection: { type: 'model', id: 'mdl-b' },
      });
      assert.equal(persisted.at(-1).model_selection.id, 'mdl-b');
      await act(async () => useSessionStore.getState().setAvailableModels(models, 'same-model'));
      assert.equal(byId('chat-panel-model-selector-label').textContent, label);
    });
  });
}

test('single-agent collaboration displays the group default while retaining a legacy pinned DTO', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    setupStableCatalog();
    setMultiModelCollaborationEnabled(true);
    useSessionStore.getState().setSelectedModelSelection(sessionId, { type: 'model_group', id: 'mgp-stable', route_id: 'rte-b' });
    await mount(createElement(ChatModelSelector));
    assert.equal(byId('chat-panel-model-selector-label').textContent, 'Stable model group');
    await click(byId('chat-panel-model-selector-trigger'));
    assert.equal(byId('chat-panel-model-selector-group-option').getAttribute('aria-checked'), 'true');
    assert.deepEqual(useSessionStore.getState().getOutgoingModelParams(sessionId), {
      model_selection: { type: 'model_group', id: 'mgp-stable', route_id: 'rte-b' },
    });
  });
});

test('model delete reference warnings prefer display names over mgp_/mdl_ ids', () => {
  const groups = [
    {
      model_group_id: 'mgp_hidden',
      display_name: '编码模型池',
      routes: [{ route_id: 'rte_a', model_id: 'mdl_a' }],
    },
  ];
  const models = [{ model_id: 'mdl_a', alias: '主模型', model_name: 'gpt-test' }];
  const refs = findModelGroupReferencesForModel('mdl_a', groups);
  assert.deepEqual(refs, [{ scope: 'model_group', scope_id: 'mgp_hidden', label: '编码模型池' }]);
  assert.equal(formatModelSelectionReferenceScopes(refs, models, groups), '编码模型池');
  assert.equal(
    formatModelSelectionReferenceScopes(
      extractModelSelectionReferences({
        payload: { references: [{ scope: 'model_group', scope_id: 'mgp_hidden', label: '编码模型池' }] },
      }),
      models,
      groups,
    ),
    '编码模型池',
  );
});

test('deleted model groups fall back to a default model instead of showing model_group_id', async (t) => {
  const persisted = [];
  t.mock.method(webClient, 'request', async (method, params) => {
    assert.equal(method, 'session.selection.set');
    persisted.push(params);
    return { success: true };
  });
  await withFixture(async ({ mount, byId }) => {
    setupStableCatalog();
    setMultiModelCollaborationEnabled(true);
    useSessionStore.getState().setSelectedModelSelection(sessionId, { type: 'model_group', id: 'mgp-deleted' });
    await act(async () => useSessionStore.getState().setAvailableModelGroups([]));
    await mount(createElement(ChatModelSelector));
    await act(async () => {});
    assert.doesNotMatch(byId('chat-panel-model-selector-label').textContent, /mgp-deleted/);
    assert.equal(byId('chat-panel-model-selector-label').textContent, 'Primary connection');
    assert.deepEqual(useSessionStore.getState().getRuntime(sessionId).selectedModelSelection, {
      type: 'model',
      id: 'mdl-a',
    });
    assert.deepEqual(persisted.at(-1), {
      session_id: sessionId,
      model_selection: { type: 'model', id: 'mdl-a' },
    });
  });
});

function groupEditor(models, routes, onSave, groupOverrides = {}) {
  return createElement(
    SettingsServicesProvider,
    { isConnected: true, connectionState: 'connected', request: async () => ({}) },
    createElement(ModelGroupDialog, {
      models,
      group: { display_name: 'Test group', routes, ...groupOverrides },
      onClose() {},
      onSave,
    }),
  );
}

function groupElement(action, variant) {
  return document.querySelector(`[data-testid="settings-model-group-dialog-${action}"]${
    variant ? `[data-variant="${variant}"]` : ''
  }`);
}

test('model group dialog adds each configured model ID once, including identical display names', async () => {
  await withFixture(async ({ mount, click }) => {
    const saved = [];
    const models = stableCatalog.map((model) => ({ ...model, alias: '' }));
    await mount(groupEditor([
      { model_id: 'free', model_name: 'free', is_free: true },
      ...models,
    ], [], async (payload) => saved.push(payload)));
    await click(groupElement('add-route-btn'));
    await click(groupElement('add-route-btn'));
    assert.equal(groupElement('add-route-btn').disabled, true);
    await click(groupElement('add-route-btn'));
    assert.equal(document.querySelectorAll('[data-testid="settings-model-group-dialog-route-row"]').length, 2);
    await click(groupElement('save-btn'));
    assert.deepEqual(saved[0].routes.map((route) => route.model_id), ['mdl-a', 'mdl-b']);
  });
});

test('model group dialog prevents flat-menu duplicates and releases a replaced model', async () => {
  await withFixture(async ({ mount, click }) => {
    const saved = [];
    await mount(groupEditor([
      ...stableCatalog, { model_id: 'mdl-c', model_name: 'Third connection' },
    ], stableGroup.routes, async (payload) => saved.push(payload)));
    await click(groupElement('route-row', 'rte-a').querySelector('button'));
    assert.equal(groupElement('model-option', 'mdl-a').disabled, false);
    assert.equal(groupElement('model-option', 'mdl-b').disabled, true);
    await click(groupElement('model-option', 'mdl-c'));
    await click(groupElement('route-row', 'rte-b').querySelector('button'));
    assert.equal(groupElement('model-option', 'mdl-a').disabled, false);
    assert.equal(groupElement('model-option', 'mdl-b').disabled, false);
    assert.equal(groupElement('model-option', 'mdl-c').disabled, true);
    await click(groupElement('save-btn'));
    assert.deepEqual(saved[0].routes.map((route) => route.model_id), ['mdl-c', 'mdl-b']);
  });
});

for (const plan of ['coding_plan', 'token_plan']) {
  test(`model group dialog prevents duplicate selection in the ${plan} submenu`, async () => {
    await withFixture(async ({ mount, click }) => {
      const saved = [];
      const models = ['a', 'b', 'c'].map((suffix) => ({
        model_id: `mdl-${suffix}`, model_name: `Model ${suffix}`, vendor_key: 'zhipu', plan,
      }));
      await mount(groupEditor(models, stableGroup.routes, async (payload) => saved.push(payload)));
      await click(groupElement('route-row', 'rte-a').querySelector('button'));
      await click(groupElement('plan-option', `${plan}:zhipu`));
      assert.equal(groupElement('plan-model-option', 'mdl-a').disabled, false);
      assert.equal(groupElement('plan-model-option', 'mdl-b').disabled, true);
      assert.equal(groupElement('plan-model-option', 'mdl-c').disabled, false);
      await click(groupElement('plan-model-option', 'mdl-b'));
      assert.equal(groupElement('plan-model-option', 'mdl-a').getAttribute('aria-checked'), 'true');
      await click(groupElement('plan-model-option', 'mdl-c'));
      await click(groupElement('save-btn'));
      assert.deepEqual(saved[0].routes.map((route) => route.model_id), ['mdl-c', 'mdl-b']);
    });
  });
}

test('model group dialog only allows re-adding a legacy duplicate after its last route is deleted', async () => {
  await withFixture(async ({ mount, click }) => {
    const saved = [];
    await mount(groupEditor(stableCatalog, [
      ...stableGroup.routes,
      { route_id: 'rte-a-second', model_id: 'mdl-a' },
    ], async (payload) => saved.push(payload)));
    assert.equal(groupElement('add-route-btn').disabled, true);
    await click(groupElement('route-delete', 'rte-a'));
    assert.equal(groupElement('add-route-btn').disabled, true);
    await click(groupElement('route-row', 'rte-b').querySelector('button'));
    assert.equal(groupElement('model-option', 'mdl-a').disabled, true);
    await click(groupElement('route-delete', 'rte-a-second'));
    assert.equal(groupElement('model-option', 'mdl-a').disabled, false);
    assert.equal(groupElement('add-route-btn').disabled, false);
    await click(groupElement('add-route-btn'));
    assert.equal(groupElement('add-route-btn').disabled, true);
    await click(groupElement('save-btn'));
    assert.deepEqual(saved[0].routes.map((route) => route.model_id), ['mdl-b', 'mdl-a']);
  });
});

test('model group dialog preserves existing duplicate route identities and overrides when saving', async () => {
  await withFixture(async ({ mount, click }) => {
    const saved = [];
    const routes = [
      ...stableGroup.routes,
      { route_id: 'rte-a-second', model_id: 'mdl-a', enabled: false,
        request_overrides: { temperature: 0.9 }, tpm: 1200, rpm: 10, timeout: 40 },
    ];
    await mount(groupEditor(stableCatalog, routes, async (payload) => saved.push(payload), {
      model_group_id: 'mgp-stable', request_config: { temperature: 0.5 },
    }));
    await click(groupElement('route-row', 'rte-a-second').querySelector('button'));
    assert.equal(groupElement('model-option', 'mdl-a').disabled, false);
    assert.equal(groupElement('model-option', 'mdl-a').getAttribute('aria-checked'), 'true');
    assert.equal(groupElement('model-option', 'mdl-b').disabled, true);
    await click(groupElement('model-option', 'mdl-a'));
    await click(groupElement('save-btn'));
    assert.deepEqual(saved[0].routes, routes);
    assert.equal(saved[0].model_group_id, 'mgp-stable');
    assert.deepEqual(saved[0].request_config, { temperature: 0.5 });
  });
});

for (const explicitSession of [false, true]) {
  test(`organization child model preserves owner selection with explicit session: ${explicitSession}`, async () => {
    const previousTeams = useTeamSelectorStore.getState();
    const previousEnabled = isTeamOrganizationUiEnabled();
    try {
      await withFixture(async ({ mount, byId }) => {
        setTeamOrganizationUiEnabled(true);
        useSessionStore.getState().setAvailableModels([
          { model_id: 'glm', model_name: 'GLM-5.3', is_default: true },
          { model_id: 'deepseek', model_name: 'deepseek-flash' },
        ], 'GLM-5.3');
        useSessionStore.getState().setMode(sessionId, 'team');
        useSessionStore.getState().setSelectedModelName(sessionId, 'GLM-5.3');
        useSessionStore.getState().setSelectedModelSelection(sessionId, { type: 'model', id: 'glm' });
        const teams = [
          { team_id: 'owner', team_name: 'owner', organization_id: 'org', is_owner: true },
          {
            team_id: 'expert', team_name: 'expert', organization_id: 'org', is_owner: false,
            execution_model_name: 'deepseek-flash', execution_model_selection: { type: 'model', id: 'deepseek' },
          },
        ];
        const runtime = { teams, selectedTeamId: 'expert', defaultTeamId: 'owner', loaded: true };
        useTeamSelectorStore.setState({ runtimes: { [sessionId]: runtime } });

        if (explicitSession) useChatStore.setState({ activeSessionId: 'another-session' });
        await mount(createElement(ChatModelSelector, explicitSession ? { sessionId } : {}));

        assert.match(byId('chat-panel-model-selector-trigger').textContent, /deepseek-flash/);
        assert.equal(byId('chat-panel-model-selector-trigger').disabled, true);
        assert.equal(useSessionStore.getState().runtimes[sessionId].selectedModelName, 'GLM-5.3');
        await act(async () => {
          useTeamSelectorStore.setState({ runtimes: { [sessionId]: { ...runtime, selectedTeamId: 'owner' } } });
        });
        assert.match(byId('chat-panel-model-selector-trigger').textContent, /GLM-5.3/);
        assert.equal(byId('chat-panel-model-selector-trigger').disabled, false);
        await act(async () => {
          useTeamSelectorStore.setState({ runtimes: { [sessionId]: runtime } });
          setTeamOrganizationUiEnabled(false);
        });
        assert.match(byId('chat-panel-model-selector-trigger').textContent, /GLM-5.3/);
      });
    } finally {
      useTeamSelectorStore.setState(previousTeams, true);
      setTeamOrganizationUiEnabled(previousEnabled);
    }
  });
}


test('design selectors hide model groups and persist an ordinary model fallback', async (t) => {
  const persisted = [];
  t.mock.method(webClient, 'request', async (method, params) => {
    assert.equal(method, 'session.selection.set');
    persisted.push(params);
    return { success: true };
  });
  await withFixture(async ({ mount, click, byId }) => {
    setupStableCatalog();
    useSessionStore.getState().setSelectedModelSelection(sessionId, { type: 'model_group', id: 'mgp-stable' });
    await mount(createElement(ChatModelSelector, { sessionId, allowModelGroups: false }));
    await act(async () => {});
    assert.deepEqual(useSessionStore.getState().getRuntime(sessionId).selectedModelSelection,
      { type: 'model', id: 'mdl-a' });
    assert.deepEqual(persisted.at(-1), { session_id: sessionId, model_selection: { type: 'model', id: 'mdl-a' } });
    await click(byId('chat-panel-model-selector-trigger'));
    assert.equal(byId('chat-panel-model-selector-group-option'), null);
    assert.equal(byId('chat-panel-model-selector-moa-option'), null);
  });
});
