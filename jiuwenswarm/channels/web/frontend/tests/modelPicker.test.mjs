import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { act, createElement, Fragment } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { JSDOM } from 'jsdom';
import { createServer } from 'vite';

// Use the production transform for the provider catalog's import.meta.glob and assets.
const vite = await createServer({
  configFile: false,
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
  const previousGlobals = new Map();
  for (const [name, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    localStorage: dom.window.localStorage,
    // 组件里会 new CustomEvent（如 requestLogin 派发的 jiuwen:auth-required），
    // 不挂到全局的话那句直接 ReferenceError
    CustomEvent: dom.window.CustomEvent,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previousGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  const previousSession = useSessionStore.getState();
  const previousChat = useChatStore.getState();
  const previousAuth = useAuthStore.getState();
  // 预置成「状态已查过、活动没开」：否则组件挂载时会真的去查登录状态，测试环境里
  // 没有后端，查询失败会起一条最长 60 秒的退避重试，整个测试文件被拖到一分半钟。
  // 需要活动开着的用例在 run 里自己改。
  useAuthStore.setState({ initialized: true, enabled: false, islogin: false });
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
    useSessionStore.getState().setSelectedModelName(sessionId, 'configured-a');
    useChatStore.setState({ activeSessionId: sessionId });
    await run({ dom, mount, click, byId });
  } finally {
    await act(async () => root.unmount());
    useSessionStore.setState(previousSession, true);
    useChatStore.setState(previousChat, true);
    useAuthStore.setState(previousAuth, true);
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

test('chat and scheduled tasks show identical grouped options, excluding secondary connections', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    await mount(createElement(ChatModelSelector), cronDrawer());
    for (const prefix of ['chat-panel-model-selector', 'cron-model-picker']) {
      await click(byId(`${prefix}-trigger`));
      const menu = byId(`${prefix}-menu`);
      assert.deepEqual(
        [...menu.querySelectorAll('.model-select__section-header')].map((node) => node.textContent),
        ['Configured Models', 'Limited-time Free Models'],
      );
      assert.deepEqual(
        [...menu.querySelectorAll('[role="menuitemradio"]')].map((node) => node.textContent),
        ['Configured A', 'Configured B', 'Free Alias'],
      );
      assert.equal(menu.querySelector('[aria-checked="true"]').dataset.variant, 'configured-a');
      await click(document.body);
    }
  });
});

test('限时免费模型：没登录拿到之前，这一栏只放一个「获取」入口', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    // 活动在跑但还没登录：免费模型一个都没有
    useAuthStore.setState({ enabled: true, initialized: true, islogin: false });
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
      ['Configured Models', 'Limited-time Free Models'],
    );
    const cta = byId('chat-panel-model-selector-free-cta');
    assert.ok(cta, '未登录时应出现「获取限时免费模型」入口');
    // 它不是可选项：选中态和模型条目不能混在一起
    assert.equal(cta.getAttribute('role'), null);
    assert.deepEqual(
      [...menu.querySelectorAll('[role="menuitemradio"]')].map((node) => node.textContent),
      ['Configured A', 'Configured B'],
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
