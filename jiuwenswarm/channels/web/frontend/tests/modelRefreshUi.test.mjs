import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

// Exercise the real settings page, free-model form, save queue and monitor.
// Custom model/group forms are isolated; their boundary still awaits onSave.
await build({
  stdin: {
    contents: `export { ModelsSettings } from './src/features/settings/modules/models/ModelsSettings';
      export { SettingsServicesProvider } from './src/features/settings/services/SettingsServicesProvider';
      export { useAuthStore } from './src/stores/authStore';`,
    resolveDir: process.cwd(), loader: 'tsx',
  },
  bundle: true, packages: 'external', platform: 'node', format: 'esm',
  outfile: 'node_modules/.cache/model-refresh/ui.js',
  define: { 'import.meta.env': '{}', 'import.meta.glob': 'testAssetGlob' },
  banner: { js: 'const testAssetGlob = () => ({});' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl', '.webp': 'dataurl' },
  plugins: [{
    name: 'model-refresh-form-boundaries',
    setup(build) {
      build.onResolve({ filter: /\.svg\?react$/ }, (args) => ({ path: args.path, namespace: 'icon' }));
      build.onLoad({ filter: /.*/, namespace: 'icon' }, () => ({
        contents: 'export default function Icon() { return null; }', loader: 'js',
      }));
      build.onResolve({ filter: /^\.\/(ModelDialog|ModelGroupDialog)$/ }, (args) => ({
        path: args.path.slice(2), namespace: 'form-boundary',
      }));
      build.onLoad({ filter: /.*/, namespace: 'form-boundary' }, ({ path }) => ({
        contents: `import { createElement } from 'react';
            export function ${path}({ onSave, onClose, saving }) {
              return createElement('button', {
                'data-testid': 'test-form-submit', disabled: saving,
                onClick: () => { void onSave(${path === 'ModelDialog'
                  ? "{ model_name: 'test', api_base: 'https://example.test', api_key: '', model_provider: 'OpenAI', reasoning_level: '' }"
                  : "{ name: 'group', routes: [] }"}).then(onClose); },
              }, 'Submit');
            }`,
        loader: 'js', resolveDir: process.cwd(),
      }));
    },
  }],
});

const { ModelsSettings, SettingsServicesProvider, useAuthStore } = await import('../node_modules/.cache/model-refresh/ui.js');
const resources = Object.fromEntries(['en', 'zh'].map((lng) => [lng, {
  translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${lng}.json`, import.meta.url), 'utf8')),
}]));
await i18next.use(initReactI18next).init({ lng: 'en', showSupportNotice: false, resources });

async function mount(t, {
  refresh = () => ({ status: 'pending', generation: 1 }), legacy = true,
  freeModels = false, saveFree = async () => {},
} = {}) {
  t.mock.timers.enable({ apis: ['setTimeout'] });
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
  // jsdom does not implement native dialog visibility; keep the real dialog/form.
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const previous = new Map();
  for (const [key, value] of Object.entries({
    window: dom.window, document: dom.window.document, localStorage: dom.window.localStorage,
    sessionStorage: dom.window.sessionStorage, IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const originalAuth = useAuthStore.getState();
  useAuthStore.setState({
    initialized: true, enabled: true, islogin: freeModels, campaignState: freeModels ? 'active' : 'off',
  });
  t.mock.method(globalThis, 'fetch', async (url) => {
    assert.equal(url, '/api/v1/auth/quota');
    return new Response(JSON.stringify({ available: false, quota: null }));
  });
  const loginModels = freeModels ? [{
    model_name: 'login-model', api_base: 'https://example.test', api_key: '', model_provider: 'OpenAI',
    reasoning_level: '', source: 'huawei-maas-login', context_window_tokens: 262144,
  }] : [];
  const calls = [];
  const request = async (method, params, options) => {
    calls.push({ method, params, options });
    if (method === 'models.refresh_status') return refresh();
    if (method === 'models.list') return { models: loginModels, model_groups: [] };
    if (method === 'config.save_all') return saveFree(params);
    if (method === 'models.upsert') return { applied_without_restart: legacy };
    if (method === 'model_groups.upsert') return {};
    if (method === 'vendors.list') return { vendors: {
      reasoning: { protocol_defaults: {}, model_fallbacks: [] }, token_plan: [], coding_plan: [], custom_api: [],
    } };
    throw new Error(`Unexpected RPC: ${method}`);
  };
  const root = createRoot(dom.window.document.getElementById('root'));
  await act(async () => root.render(createElement(SettingsServicesProvider, {
    request, isConnected: true, connectionState: 'connected',
  }, createElement(ModelsSettings, {
    tabs: [{ id: 'models', labelKey: 'settingsPanel.models.customModels' },
      { id: 'groups', labelKey: 'settingsPanel.models.modelGroupsManagement' }], defaultTab: 'models',
  }))));
  t.after(async () => {
    await act(async () => root.unmount());
    useAuthStore.setState(originalAuth);
    dom.window.close();
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  const find = (id) => dom.window.document.querySelector(`[data-testid="${id}"]`);
  return {
    find, calls,
    freeDialog: () => dom.window.document.querySelector('.free-models-config-dialog'),
    click: async (element) => act(async () => element.click()),
    tick: async (ms) => act(async () => t.mock.timers.tick(ms)),
  };
}

test('pending and failed feedback survives toast expiry and is visible on both tabs', async (t) => {
  let status = 'pending';
  const view = await mount(t, { refresh: () => ({ status, generation: 1 }) });
  assert.equal(view.find('settings-models-refresh-status')?.dataset.variant, 'pending');
  status = 'failed';
  await view.tick(2000);
  assert.equal(view.find('settings-models-refresh-status')?.dataset.variant, 'failed');
  assert.match(view.find('settings-models-refresh-status').textContent, /saved|retained/i);
  await view.click(view.find('settings-models-tab').parentElement.querySelector('[data-variant="groups"]'));
  await view.tick(10000);
  assert.equal(view.find('settings-models-refresh-status')?.dataset.variant, 'failed');
});

for (const legacy of [true, false]) {
  test(`save closes the form without awaiting status; legacy accepted=${legacy}`, async (t) => {
    let stall = false;
    const view = await mount(t, { legacy, refresh: () => stall
      ? new Promise(() => {}) : { status: 'applied', generation: 0 } });
    stall = true;
    await view.click(view.find('settings-models-add-btn'));
    await view.click(view.find('test-form-submit'));
    assert.equal(view.find('test-form-submit'), null);
    assert.equal(view.find('settings-models-add-btn').disabled, false);
    assert.match(view.find('settings-models-toast').textContent, legacy ? /^Saved$/ : /restart/i);
    assert.equal(view.calls.filter(({ method }) => method === 'models.refresh_status').length, 2);
    await view.tick(5000);
    assert.equal(view.find('settings-models-refresh-status')?.dataset.variant, 'unavailable');
  });
}

test('model group save shares background monitoring and uses a generic saved toast', async (t) => {
  const view = await mount(t);
  await view.click(view.find('settings-models-tab').parentElement.querySelector('[data-variant="groups"]'));
  await view.click(view.find('settings-models-groups-add-btn'));
  await view.click(view.find('test-form-submit'));
  assert.equal(view.find('test-form-submit'), null);
  assert.equal(view.find('settings-models-toast').textContent, 'Saved');
  assert.equal(view.calls.filter(({ method }) => method === 'models.refresh_status').length, 2);
});

test('status retry recovers from unavailable to persistent applied feedback', async (t) => {
  let fail = true;
  const view = await mount(t, { refresh: () => {
    if (fail) throw new Error('private transport text');
    return { status: 'applied', generation: 4 };
  } });
  assert.equal(view.find('settings-models-refresh-status')?.dataset.variant, 'unavailable');
  assert.doesNotMatch(view.find('settings-models-refresh-status').textContent, /private/);
  fail = false;
  await view.click(view.find('settings-models-refresh-retry-btn'));
  await view.tick(10000);
  assert.equal(view.find('settings-models-refresh-status')?.dataset.variant, 'applied');
});

test('applied generation zero after a Worker restart does not claim snapshots are ready', async (t) => {
  const view = await mount(t, { refresh: () => ({ status: 'applied', generation: 0 }) });
  assert.equal(Boolean(view.find('settings-models-refresh-status')), false);
  assert.equal(Boolean(view.find('settings-models-toast')), false);
});

for (const initialGeneration of [0, 4]) {
  test(`real free-model save resumes monitoring after applied generation ${initialGeneration}`, async (t) => {
    let status = 'applied';
    let generation = initialGeneration;
    const view = await mount(t, {
      freeModels: true,
      refresh: () => ({ status, generation }),
      saveFree: async () => { status = 'pending'; generation = initialGeneration + 1; },
    });
    await view.tick(10000);
    assert.equal(view.calls.filter(({ method }) => method === 'models.refresh_status').length, 1);
    await view.click(view.find('settings-free-models-config'));
    assert.equal(view.freeDialog().open, true);
    await view.click(view.find('settings-context-window-presets').querySelector('[data-variant="512K"]'));
    await view.click(view.freeDialog().querySelector('.free-models-config__footer button:last-child'));
    assert.equal(view.freeDialog().open, false);
    assert.deepEqual(view.calls.find(({ method }) => method === 'config.save_all').params, {
      login_model_settings: { 'login-model': { context_window: 524288 } },
    });
    assert.equal(view.find('settings-models-refresh-status')?.dataset.variant, 'pending');
    status = 'failed';
    await view.tick(2000);
    assert.equal(view.find('settings-models-refresh-status')?.dataset.variant, 'failed');
  });
}

test('real free-model save closes without awaiting a stalled status query', async (t) => {
  let saved = false;
  const view = await mount(t, {
    freeModels: true, saveFree: async () => { saved = true; },
    refresh: () => saved ? new Promise(() => {}) : { status: 'applied', generation: 4 },
  });
  await view.click(view.find('settings-free-models-config'));
  await view.click(view.find('settings-context-window-presets').querySelector('[data-variant="512K"]'));
  await view.click(view.freeDialog().querySelector('.free-models-config__footer button:last-child'));
  assert.equal(view.freeDialog().open, false);
  await view.tick(5000);
  assert.equal(view.find('settings-models-refresh-status')?.dataset.variant, 'unavailable');
});

test('rejected free-model save keeps the dialog open without restarting refresh observation', async (t) => {
  const view = await mount(t, {
    freeModels: true, refresh: () => ({ status: 'applied', generation: 4 }),
    saveFree: async () => { throw new Error('Save failed'); },
  });
  await view.click(view.find('settings-free-models-config'));
  await view.click(view.find('settings-context-window-presets').querySelector('[data-variant="512K"]'));
  await view.click(view.freeDialog().querySelector('.free-models-config__footer button:last-child'));
  assert.equal(view.freeDialog().open, true);
  assert.equal(view.freeDialog().querySelector('[role="alert"]').textContent, 'Save failed');
  assert.equal(view.calls.filter(({ method }) => method === 'models.refresh_status').length, 1);
});

test('closing the free-model form without changes does not write or restart observation', async (t) => {
  const view = await mount(t, { freeModels: true, refresh: () => ({ status: 'applied', generation: 4 }) });
  await view.click(view.find('settings-free-models-config'));
  await view.click(view.freeDialog().querySelector('.free-models-config__footer button:last-child'));
  assert.equal(view.freeDialog().open, false);
  assert.equal(view.calls.some(({ method }) => method === 'config.save_all'), false);
  assert.equal(view.calls.filter(({ method }) => method === 'models.refresh_status').length, 1);
});
