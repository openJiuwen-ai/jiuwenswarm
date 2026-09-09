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
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previousGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  const previousSession = useSessionStore.getState();
  const previousChat = useChatStore.getState();
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
        ['Configured Models', 'Free Models'],
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
    await click(byId('cron-model-picker-menu').querySelector('[data-variant="free-model"]'));
    assert.equal(byId('cron-model-picker-menu'), null);
    assert.equal(byId('cron-model-picker-trigger').textContent, 'Free Alias');
    assert.equal(byId('chat-panel-model-selector-trigger').textContent, 'Configured A');
    assert.equal(useSessionStore.getState().getEffectiveModelName(sessionId), 'configured-a');
    await click(byId('cron-drawer-submit-btn'));
    assert.deepEqual(submitted, { ...initialForm, modelName: 'free-model' });
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
    await click(byId('chat-panel-model-selector-menu').querySelector('[data-variant="free-model"]'));
    assert.equal(useSessionStore.getState().getEffectiveModelName(sessionId), 'free-model');
    assert.equal(byId('chat-panel-model-selector-trigger').textContent, 'Free Alias');
    assert.equal(byId('cron-model-picker-trigger').textContent, 'Configured A');
    await click(byId('cron-drawer-submit-btn'));
    assert.deepEqual(submitted, initialForm);
  });
});

test('a historical chat alias still resolves to the selected model after extraction', async () => {
  await withFixture(async ({ mount, click, byId }) => {
    useSessionStore.getState().setSelectedModelName(sessionId, 'Free Alias');
    await mount(createElement(ChatModelSelector));
    assert.equal(byId('chat-panel-model-selector-trigger').textContent, 'Free Alias');
    await click(byId('chat-panel-model-selector-trigger'));
    assert.equal(document.querySelector('[aria-checked="true"]').dataset.variant, 'free-model');
    assert.equal(useSessionStore.getState().getEffectiveModelName(sessionId), 'free-model');
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
    await click(byId('cron-model-picker-menu').querySelector('[data-variant="free-model"]'));
    assert.equal(byId('cron-mode-trigger').dataset.variant, 'team');
    assert.equal(byId('cron-model-picker-trigger').textContent, 'Free Alias');
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
