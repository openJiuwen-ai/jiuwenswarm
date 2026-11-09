import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';

const catalog = {
  token_plan: [],
  coding_plan: [],
  custom_api: [],
  reasoning: {
    protocol_defaults: { openai: { options: [], recommended: null }, anthropic: { options: [], recommended: null } },
    model_fallbacks: [],
  },
};
const existing = {
  model_id: 'mdl_existing', alias: 'Existing model', model_name: 'existing-model',
  model_provider: 'OpenAI', api_base: 'http://127.0.0.1:1234/v1', api_key: '',
  has_api_key: true, reasoning_level: '', context_window_tokens: 262144,
};
const i18n = i18next.createInstance();
const previousGlobals = new Map();
let dom;
let createRoot;
let ModelsSettings;
let SettingsServicesProvider;
let I18nextProvider;

before(async () => {
  dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true, url: 'http://localhost' });
  for (const [name, value] of Object.entries({
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, Node: dom.window.Node, IS_REACT_ACT_ENVIRONMENT: true,
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
        export { ModelsSettings } from './src/features/settings/modules/models/ModelsSettings';
        export { SettingsServicesProvider } from './src/features/settings/services/SettingsServicesProvider';
        export { useAuthStore } from './src/stores/authStore';
      `,
      resolveDir: fileURLToPath(new URL('../', import.meta.url)), loader: 'ts',
    },
    bundle: true, write: false, platform: 'node', format: 'cjs', packages: 'external',
    loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
    plugins: [{
      name: 'decorative-svg-components',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'decorative-svg' }));
        builder.onLoad({ filter: /.*/, namespace: 'decorative-svg' }, () => ({ contents: 'export default () => null;', loader: 'js' }));
      },
    }],
    define: { 'import.meta.glob': 'testAssetGlob', 'import.meta.env': '{}' },
    banner: { js: 'const testAssetGlob = () => ({});' },
  });
  const compiled = { exports: {} };
  const require = createRequire(import.meta.url);
  new Function('require', 'module', 'exports', result.outputFiles[0].text)(require, compiled, compiled.exports);
  ({ ModelsSettings, SettingsServicesProvider } = compiled.exports);
  compiled.exports.useAuthStore.setState({ initialized: true, enabled: false, campaignState: 'off' });
  ({ I18nextProvider } = require('react-i18next'));
  await i18n.init({
    lng: 'en', showSupportNotice: false,
    resources: { en: { translation: JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8')) } },
  });
});

after(() => {
  dom?.window.close();
  for (const [name, descriptor] of previousGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function get(id, variant) {
  const node = document.querySelector(`[data-testid="${id}"]${variant ? `[data-variant="${variant}"]` : ''}`);
  assert.ok(node, `Missing ${id} ${variant ?? ''}`);
  return node;
}

function field(name) {
  const node = get('settings-model-dialog-field', name).querySelector('input');
  assert.ok(node, `Missing input ${name}`);
  return node;
}

async function click(node) { await act(async () => node.click()); }

async function fill(node, value) {
  assert.equal(node.disabled, false);
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(node, value);
    node.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
}

async function mount(t, options = {}) {
  const models = deferred();
  const vendors = deferred();
  const calls = [];
  const saved = [];
  let modelLoads = 0;
  let vendorLoads = 0;
  const request = async (method, params) => {
    calls.push({ method, params });
    if (method === 'models.list') {
      modelLoads += 1;
      return modelLoads === 1 ? models.promise : { models: [existing, ...saved] };
    }
    if (method === 'vendors.list') {
      vendorLoads += 1;
      return vendorLoads === 1 ? vendors.promise : { vendors: catalog };
    }
    if (method === 'config.validate_model') return {};
    if (method === 'models.upsert') {
      const resource = params.model;
      saved.push({
        model_id: 'mdl_added', alias: resource.alias,
        model_name: resource.model_client_config.model_name, model_provider: 'OpenAI',
        api_base: resource.model_client_config.api_base, api_key: '', has_api_key: true, reasoning_level: '',
      });
      return { applied_without_restart: true };
    }
    throw new Error(`Unexpected request ${method}`);
  };
  const root = createRoot(document.getElementById('root'));
  t.after(async () => { await act(async () => root.unmount()); });
  async function render(connected = true) {
    await act(async () => root.render(
      createElement(I18nextProvider, { i18n },
        createElement(SettingsServicesProvider, { isConnected: connected, connectionState: connected ? 'connected' : 'disconnected', request },
          createElement(ModelsSettings, {
            tabs: [{ id: 'models', labelKey: 'settingsPanel.models.customModels' }], defaultTab: 'models',
          }),
        ),
      ),
    ));
  }
  await render();
  assert.equal(get('settings-models-add-btn').disabled, false, 'opening a blank draft must not wait for models.list');
  await click(get('settings-models-add-btn'));
  await click(get('settings-model-provider-select-trigger'));
  await click(get('settings-model-provider-select-option', 'custom'));
  await fill(field('api_base'), 'http://127.0.0.1:1235/v1');
  await fill(field('api_key'), 'local-test-key');
  await fill(get('settings-model-name-field-input'), 'new-model');
  await fill(field('alias'), options.alias ?? 'New model');
  return { models, vendors, calls, saved, render };
}

function assertDraft(alias = 'New model') {
  assert.equal(field('alias').value, alias);
  assert.equal(field('api_key').value, 'local-test-key');
  assert.equal(field('api_base').value, 'http://127.0.0.1:1235/v1');
  assert.equal(get('settings-model-name-field-input').value, 'new-model');
}

function assertNoMutation(ui) {
  assert.equal(get('settings-model-dialog-save-btn').disabled, true);
  assert.equal(ui.calls.filter(({ method }) => method === 'config.validate_model' || method === 'models.upsert').length, 0);
}

test('new draft is editable before SDK-backed reads finish; catalog-first completion preserves it and gates save', async (t) => {
  const ui = await mount(t);
  assertDraft();
  assertNoMutation(ui);
  assert.equal(document.querySelector('[data-testid="settings-model-dialog-catalog-loading"]'), null);
  assert.equal(document.querySelector('[data-testid="settings-model-dialog-models-loading"]'), null);
  await act(async () => ui.vendors.resolve({ vendors: catalog }));
  assertDraft();
  assertNoMutation(ui);
  await act(async () => ui.models.resolve({ models: [existing] }));
  assertDraft();
  assert.equal(get('settings-model-dialog-save-btn').disabled, false);
  await click(get('settings-model-dialog-save-btn'));
  assert.equal(ui.calls.filter(({ method }) => method === 'config.validate_model').length, 1);
  const writes = ui.calls.filter(({ method }) => method === 'models.upsert');
  assert.equal(writes.length, 1);
  assert.equal(writes[0].params.model.model_client_config.api_key, 'local-test-key');
  assert.equal(writes[0].params.model.model_client_config.model_name, 'new-model');
  assert.equal(writes[0].params.model.model_id, undefined);
  assert.equal(document.querySelector('[data-testid="settings-model-dialog"]'), null);
  assert.match(get('settings-models-list').textContent, /Existing model/);
  assert.match(get('settings-models-list').textContent, /New model/);
});

test('model-list-first completion still waits for the vendor catalog and retains user input', async (t) => {
  const ui = await mount(t);
  await act(async () => ui.models.resolve({ models: [existing] }));
  assertDraft();
  assertNoMutation(ui);
  await act(async () => ui.vendors.resolve({ vendors: catalog }));
  assertDraft();
  assert.equal(get('settings-model-dialog-save-btn').disabled, false);
});

test('a failed initial model list cannot be mistaken for an empty list; retry preserves the draft', async (t) => {
  const ui = await mount(t);
  await act(async () => { ui.vendors.resolve({ vendors: catalog }); ui.models.reject(new Error('Startup interrupted')); });
  assertNoMutation(ui);
  assertDraft();
  get('settings-model-dialog-models-error');
  await click(get('settings-model-dialog-models-retry-btn'));
  assertDraft();
  assert.equal(get('settings-model-dialog-save-btn').disabled, false);
});

test('invalid initial model payload keeps save blocked until a valid retry', async (t) => {
  const ui = await mount(t);
  await act(async () => { ui.vendors.resolve({ vendors: catalog }); ui.models.resolve({ models: 'invalid' }); });
  assertNoMutation(ui);
  assertDraft();
  await click(get('settings-model-dialog-models-retry-btn'));
  assert.equal(get('settings-model-dialog-save-btn').disabled, false);
});

test('failed catalog discovery keeps fields editable and retry preserves the entire draft', async (t) => {
  const ui = await mount(t);
  await act(async () => { ui.models.resolve({ models: [existing] }); ui.vendors.reject(new Error('Catalog interrupted')); });
  assertNoMutation(ui);
  await fill(field('alias'), 'Changed while unavailable');
  await click(get('settings-model-dialog-catalog-retry-btn'));
  assertDraft('Changed while unavailable');
  assert.equal(get('settings-model-dialog-save-btn').disabled, false);
});

test('a late existing model with the same display name is still rejected before connection testing or writing', async (t) => {
  const ui = await mount(t, { alias: 'Existing model' });
  await act(async () => { ui.models.resolve({ models: [existing] }); ui.vendors.resolve({ vendors: catalog }); });
  await click(get('settings-model-dialog-save-btn'));
  assert.equal(ui.calls.filter(({ method }) => method === 'config.validate_model' || method === 'models.upsert').length, 0);
  assertDraft('Existing model');
  assert.match(get('settings-model-dialog').textContent, new RegExp(i18n.t('settingsPanel.models.validation.aliasConflict')));
});

test('reconnecting during initialization reloads both reads and ignores stale responses without clearing the draft', async (t) => {
  const ui = await mount(t);
  await ui.render(false);
  assertNoMutation(ui);
  assertDraft();
  await ui.render(true);
  assertDraft();
  assert.equal(get('settings-model-dialog-save-btn').disabled, false);
  await act(async () => { ui.models.resolve({ models: [] }); ui.vendors.resolve({ vendors: { invalid: true } }); });
  assertDraft();
  assert.equal(get('settings-model-dialog-save-btn').disabled, false);
  assert.match(get('settings-models-list').textContent, /Existing model/);
  assert.equal(ui.calls.filter(({ method }) => method === 'vendors.list').length, 2);
});

test('canceling a loading draft retains the existing discard confirmation and ignores later responses', async (t) => {
  const ui = await mount(t);
  await click(get('settings-model-dialog-cancel-btn'));
  const discard = [...document.querySelectorAll('dialog[open]')].find((node) => node.textContent.includes(i18n.t('settingsPanel.dialog.discardTitle')));
  assert.ok(discard);
  const confirm = [...discard.querySelectorAll('button')].find((node) => node.textContent.trim() === i18n.t('common.confirm'));
  await click(confirm);
  await act(async () => { ui.models.resolve({ models: [existing] }); ui.vendors.resolve({ vendors: catalog }); });
  assert.equal(document.querySelector('[data-testid="settings-model-dialog"]'), null);
  assert.equal(ui.saved.length, 0);
});
