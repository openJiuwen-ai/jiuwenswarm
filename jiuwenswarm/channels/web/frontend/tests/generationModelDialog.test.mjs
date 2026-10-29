import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';

const base = 'https://ark.cn-beijing.volces.com/api/v3';
const preset = (plan, apiBase) => ({
  vendor_key: 'volcengine',
  display_name: '火山引擎',
  plan,
  client_provider: 'OpenAI',
  api_base: apiBase,
  default_model: 'seed-chat',
  model_options: ['seed-chat'],
  icon_key: 'doubao',
  models_endpoint: `${apiBase}/models`,
  models_needs_key: true,
  supports_anthropic: false,
  reasoning_capabilities: {},
  reasoning_rules: [],
});
const catalog = {
  token_plan: [],
  coding_plan: [preset('coding_plan', 'https://ark.cn-beijing.volces.com/api/coding/v3')],
  custom_api: [preset('custom_api', base)],
  reasoning: {
    protocol_defaults: { openai: { options: [], recommended: null }, anthropic: { options: [], recommended: null } },
    model_fallbacks: [],
  },
};
const remoteModels = ['remote-generation-one', 'remote-generation-two'];
const remoteResult = () => ({ source: 'remote', models: remoteModels });
const i18n = i18next.createInstance();
const previousGlobals = new Map();
let dom;
let createRoot;
let GenerationModelConfigDialog;
let SettingsServicesProvider;
let I18nextProvider;

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
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  ({ createRoot } = await import('react-dom/client'));
  const result = await build({
    stdin: {
      contents: `
        export { GenerationModelConfigDialog } from './src/features/settings/modules/agent/GenerationModelConfigDialog';
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
    define: { 'import.meta.glob': 'testAssetGlob', 'import.meta.env': '{}' },
    banner: { js: 'const testAssetGlob = () => ({});' },
  });
  const compiled = { exports: {} };
  const require = createRequire(import.meta.url);
  new Function('require', 'module', 'exports', result.outputFiles[0].text)(require, compiled, compiled.exports);
  ({ GenerationModelConfigDialog, SettingsServicesProvider } = compiled.exports);
  ({ I18nextProvider } = require('react-i18next'));
  await i18n.init({
    lng: 'en',
    showSupportNotice: false,
    resources: {
      en: { translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8')) },
    },
  });
});

after(() => {
  dom?.window.close();
  for (const [name, descriptor] of previousGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

function get(testId) {
  const node = document.querySelector(`[data-testid="${testId}"]`);
  assert.ok(node, `Missing ${testId}`);
  return node;
}

async function mountDialog(t, slot, apiBase, fetchResult = async () => remoteResult(), configOverrides = {}) {
  const requests = [];
  const saved = [];
  const config = {
    [`${slot}_provider`]: 'VolcEngine',
    [`${slot}_protocol`]: 'modelark',
    [`${slot}_api_base`]: apiBase,
    [`${slot}_api_key`]: 'test-key',
    [`${slot}_model`]: 'saved-model',
    [`${slot}_context_window_tokens`]: 'not-used-by-generation',
    ...configOverrides,
  };
  const request = async (method, params) => {
    requests.push({ method, params });
    if (method === 'vendors.list') return { vendors: catalog };
    if (method === 'vendors.fetch_models') return fetchResult();
    throw new Error(`Unexpected request: ${method}`);
  };
  const root = createRoot(document.getElementById('root'));
  t.after(async () => { await act(async () => root.unmount()); });
  await act(async () => root.render(
    createElement(I18nextProvider, { i18n },
      createElement(SettingsServicesProvider, { isConnected: true, connectionState: 'connected', request },
        createElement(GenerationModelConfigDialog, {
          slot,
          titleKey: slot === 'visual_gen' ? 'settingsPanel.agent.visualGenConfigTitle' : 'settingsPanel.agent.videoGenConfigTitle',
          config,
          enableOnSave: false,
          save: async (updates) => { saved.push(updates); },
          onClose: () => undefined,
        }),
      ),
    ),
  ));
  return { requests, saved };
}

for (const slot of ['visual_gen', 'video_gen']) {
  test(`${slot} rejects a custom modelark proxy before saving`, async (t) => {
    const proxy = 'https://my-proxy.example/api/v3';
    const { saved } = await mountDialog(t, slot, proxy, async () => remoteResult(), {
      [`${slot}_provider`]: 'My proxy',
    });
    assert.ok(get('settings-model-provider-select-value').textContent.includes(i18n.t('settingsPanel.models.customVendor')));
    await act(async () => get('settings-agent-config-dialog-save-btn').click());
    assert.equal(saved.length, 0);
    assert.ok(document.body.textContent.includes(i18n.t('settingsPanel.agent.arkGenerationBaseInvalid')));
  });

  for (const apiBase of [base, 'https://ark.ap-southeast.bytepluses.com/api/v3', 'https://ark.cn-beijing.volces.com']) {
    test(`${slot} restores ${apiBase} with full discovery and preserves saved credentials`, async (t) => {
      const { requests, saved } = await mountDialog(t, slot, apiBase);
      assert.ok(get('settings-model-provider-select-value').textContent.includes(i18n.t('settingsPanel.models.vendors.volcengine')));
      assert.equal(document.querySelector('[data-variant="api_base"] input').value, apiBase);
      assert.equal(document.querySelector('[data-variant="api_key"] input').value, 'test-key');
      assert.equal(document.querySelector('[data-variant="context_window_tokens"]'), null);
      assert.equal(get('settings-model-name-field-refresh-btn').disabled, false);
      await act(async () => get('settings-model-name-field-input').focus());
      const fetch = requests.find((entry) => entry.method === 'vendors.fetch_models');
      assert.deepEqual(fetch?.params, {
        vendor_key: 'volcengine', plan: 'custom_api', api_key: 'test-key', api_base: apiBase,
        modality: slot === 'visual_gen' ? 'image' : 'video',
      });
      const options = [...document.querySelectorAll('[data-testid="settings-model-name-field-option"]')];
      assert.deepEqual(options.map((node) => node.dataset.variant), ['saved-model', ...remoteModels]);
      await act(async () => get('settings-agent-config-dialog-save-btn').click());
      assert.deepEqual(saved, [{
        [`${slot}_provider`]: 'VolcEngine', [`${slot}_protocol`]: 'modelark',
        [`${slot}_api_base`]: apiBase, [`${slot}_api_key`]: 'test-key', [`${slot}_model`]: 'saved-model',
      }]);
    });
  }

  for (const outcome of ['success', 'failure']) {
    test(`${slot} preserves manual model input during delayed discovery ${outcome}`, async (t) => {
      let resolve;
      let reject;
      const pending = new Promise((accept, decline) => { resolve = accept; reject = decline; });
      const { saved } = await mountDialog(t, slot, base, () => pending);
      const input = get('settings-model-name-field-input');
      await act(async () => input.focus());
      assert.equal(get('settings-model-name-field-refresh-btn').dataset.loading, 'true');
      await act(async () => {
        const setValue = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set;
        setValue.call(input, 'manual-new-model');
        input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
      });
      await act(async () => {
        if (outcome === 'success') resolve(remoteResult());
        else reject(new Error('discovery unavailable'));
      });
      assert.equal(input.value, 'manual-new-model');
      const customOption = document.querySelector('[data-testid="settings-model-name-field-option"][data-variant="manual-new-model"]');
      assert.ok(customOption);
      await act(async () => customOption.click());
      await act(async () => get('settings-agent-config-dialog-save-btn').click());
      assert.equal(saved.length, 1);
      assert.equal(saved[0][`${slot}_model`], 'manual-new-model');
      assert.equal(saved[0][`${slot}_protocol`], 'modelark');
    });
  }
}

test('failed Ark discovery waits for an explicit refresh or changed credentials', async (t) => {
  const { requests } = await mountDialog(t, 'visual_gen', base, async () => {
    throw new Error('discovery unavailable');
  });
  const input = get('settings-model-name-field-input');
  await act(async () => input.focus());
  assert.equal(requests.filter((entry) => entry.method === 'vendors.fetch_models').length, 1);
  await act(async () => { input.blur(); input.focus(); });
  assert.equal(requests.filter((entry) => entry.method === 'vendors.fetch_models').length, 1);
  await act(async () => get('settings-model-name-field-refresh-btn').click());
  assert.equal(requests.filter((entry) => entry.method === 'vendors.fetch_models').length, 2);
  const apiKey = document.querySelector('[data-variant="api_key"] input');
  await act(async () => {
    const setValue = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set;
    setValue.call(apiKey, 'updated-key');
    apiKey.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
  await act(async () => { input.blur(); input.focus(); });
  assert.equal(requests.filter((entry) => entry.method === 'vendors.fetch_models').length, 3);
  assert.equal(requests.at(-1).params.api_key, 'updated-key');
});
