import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import {
  buildReasoningOptions,
  isReasoningLevelSupported,
  parseReasoningCapability,
  parseReasoningRules,
  resolveModelReasoning,
} from '../node_modules/.cache/model-reasoning/modelReasoning.js';
import {
  parseVendorCatalog,
  reconcileModelReasoning,
  createModelDraft,
  modelDraftToEntry,
} from '../node_modules/.cache/model-reasoning/modelAdapters.js';

const capability = (options, recommended = null) => ({ options, recommended });
const toggle = capability(['off', 'on'], 'on');
const strengths = capability(['off', 'high', 'max'], 'max');
const hidden = capability([]);
const both = (value) => ({ openai: value, anthropic: value });
const rule = (patterns, value) => ({ patterns, capabilities: both(value) });
const vendor = {
  vendor_key: 'baidu',
  display_name: 'Baidu',
  plan: 'custom_api',
  client_provider: 'OpenAI',
  api_base: 'https://qianfan.baidubce.com/v2',
  endpoint_profile: null,
  default_model: 'glm-5.2',
  model_options: ['glm-5.2', 'codegeex-4'],
  icon_key: 'baidu',
  models_endpoint: null,
  models_needs_key: false,
  supports_anthropic: true,
  anthropic_base: 'https://qianfan.baidubce.com/anthropic',
  anthropic_client_provider: 'Anthropic',
  reasoning_capabilities: {
    'glm-5.2': both(toggle),
    'codegeex-4': both(hidden),
  },
  reasoning_rules: [rule(['glm-5.2*'], toggle), rule(['glm-5*'], hidden)],
};
const catalogPayload = {
  token_plan: [],
  coding_plan: [],
  custom_api: [vendor],
  reasoning: {
    protocol_defaults: {
      openai: capability(['off', 'low', 'medium', 'high']),
      anthropic: capability(['off', 'low', 'medium', 'high', 'max']),
    },
    model_fallbacks: [
      rule(['glm-5.2*'], strengths),
      rule(['codegeex-4*', 'qwen3-vl-*-thinking*'], hidden),
      rule(['kimi-k3*'], capability(['low', 'high', 'max'], 'high')),
    ],
  },
};
const catalog = parseVendorCatalog(catalogPayload);
const preset = catalog.custom_api[0];

test('reasoning options preserve backend values and order; labels stay raw without a resolver', () => {
  const capability = parseReasoningCapability({ options: ['on', 'off', 'new-tier'], recommended: 'on' });
  assert.deepEqual(buildReasoningOptions(capability, '使用默认值'), [
    { value: '', label: '使用默认值' },
    { value: 'on', label: 'on' },
    { value: 'off', label: 'off' },
    { value: 'new-tier', label: 'new-tier' },
  ]);
  assert.deepEqual(buildReasoningOptions(capability, 'Use default').slice(1), [
    { value: 'on', label: 'on' },
    { value: 'off', label: 'off' },
    { value: 'new-tier', label: 'new-tier' },
  ]);
});

test('reasoning option labels use resolver for known tiers and fall back to raw value', () => {
  const capability = parseReasoningCapability({
    options: ['off', 'low', 'medium', 'high', 'new-tier'],
    recommended: 'low',
  });
  const labels = {
    off: '关闭',
    low: '低',
    medium: '中',
    high: '高',
  };
  assert.deepEqual(
    buildReasoningOptions(capability, '使用默认值', (value) => labels[value] ?? value),
    [
      { value: '', label: '使用默认值' },
      { value: 'off', label: '关闭' },
      { value: 'low', label: '低' },
      { value: 'medium', label: '中' },
      { value: 'high', label: '高' },
      { value: 'new-tier', label: 'new-tier' },
    ],
  );
});

test('empty capability hides the select, while default remains a valid persistence value', () => {
  const capability = parseReasoningCapability({ options: [], recommended: null });
  assert.deepEqual(buildReasoningOptions(capability, '使用默认值'), []);
  assert.equal(isReasoningLevelSupported('', capability), true);
  assert.equal(isReasoningLevelSupported('off', capability), false);
  assert.equal(isReasoningLevelSupported('on', capability), false);
});

test('validation accepts only the returned options plus default and never selects the recommendation', () => {
  const capability = parseReasoningCapability({ options: ['low', 'high', 'max'], recommended: 'high' });
  for (const value of ['', 'low', 'high', 'max']) {
    assert.equal(isReasoningLevelSupported(value, capability), true);
  }
  for (const value of ['off', 'on', 'medium', 'HIGH']) {
    assert.equal(isReasoningLevelSupported(value, capability), false);
  }
  assert.equal(buildReasoningOptions(capability, '使用默认值')[0].value, '');
});

test('malformed capabilities fail explicitly instead of enabling a fixed option list', () => {
  for (const value of [
    null,
    [],
    {},
    { options: 'off', recommended: null },
    { options: [true], recommended: null },
    { options: [''], recommended: null },
    { options: [' off'], recommended: null },
    { options: ['off', 'off'], recommended: null },
    { options: ['off'], recommended: 'on' },
    { options: ['off'], recommended: false },
    { options: ['off'] },
  ]) {
    assert.throws(() => parseReasoningCapability(value), /INVALID_REASONING_CAPABILITY/);
  }
});

test('vendor catalog preserves global, exact and provider-scoped capabilities', () => {
  assert.deepEqual(catalog.reasoning, catalogPayload.reasoning);
  assert.deepEqual(preset.reasoning_capabilities, vendor.reasoning_capabilities);
  assert.deepEqual(preset.reasoning_rules, vendor.reasoning_rules);
  for (const payload of [
    { ...catalogPayload, reasoning: undefined },
    { ...catalogPayload, reasoning: { ...catalogPayload.reasoning, protocol_defaults: { openai: toggle } } },
    { ...catalogPayload, custom_api: [{ ...vendor, reasoning_capabilities: null }] },
    { ...catalogPayload, custom_api: [{ ...vendor, reasoning_rules: null }] },
    { ...catalogPayload, custom_api: [{ ...vendor, reasoning_rules: [{ patterns: ['glm*'], capabilities: {} }] }] },
  ]) {
    assert.throws(() => parseVendorCatalog(payload), /INVALID_VENDOR_CATALOG/);
  }
});

test('exact model and protocol capabilities take precedence, including an empty option list', () => {
  assert.deepEqual(resolveModelReasoning(catalog, preset, 'glm-5.2', 'openai'), toggle);
  assert.deepEqual(resolveModelReasoning(catalog, preset, 'codegeex-4', 'openai'), hidden);
  const protocolPreset = {
    ...preset,
    reasoning_capabilities: {
      'glm-5.2': { openai: toggle, anthropic: strengths },
    },
  };
  assert.deepEqual(resolveModelReasoning(catalog, protocolPreset, 'glm-5.2', 'anthropic'), strengths);
});

test('provider rules preserve first-match order and match only full names, as the backend does', () => {
  assert.deepEqual(resolveModelReasoning(catalog, preset, ' GLM-5.2-latest ', 'openai'), toggle);
  assert.deepEqual(resolveModelReasoning(catalog, preset, 'glm-5.1', 'openai'), hidden);
  // Short-name matching belongs to the global rules, not the provider rules.
  assert.deepEqual(resolveModelReasoning(catalog, preset, 'vendor/glm-5.2', 'openai'), strengths);
});

test('custom and account models use returned global rules and protocol defaults', () => {
  assert.deepEqual(resolveModelReasoning(catalog, undefined, 'vendor/glm-5.2-v2', 'openai'), strengths);
  assert.deepEqual(resolveModelReasoning(catalog, undefined, 'kimi-k3', 'openai').options, ['low', 'high', 'max']);
  assert.deepEqual(
    resolveModelReasoning(catalog, undefined, 'unknown', 'anthropic'),
    catalog.reasoning.protocol_defaults.anthropic,
  );
  assert.equal(resolveModelReasoning({ ...catalog, reasoning: null }, preset, 'glm-5.2', 'openai'), null);
  assert.equal(resolveModelReasoning(catalog, preset, ' ', 'openai'), null);
});

test('catalog star patterns match full strings, literal punctuation and internal wildcards', () => {
  assert.deepEqual(resolveModelReasoning(catalog, undefined, 'qwen3-vl-235b-thinking-latest', 'openai'), hidden);
  for (const name of ['qwen3-vl-235b', 'not-qwen3-vl-235b-thinking', 'glm-5x2']) {
    assert.deepEqual(
      resolveModelReasoning(catalog, undefined, name, 'openai'),
      catalog.reasoning.protocol_defaults.openai,
    );
  }
  for (const pattern of ['*', '**', '*a*b*', 'x*y*z']) {
    assert.equal(parseReasoningRules([rule([pattern], hidden)])[0].patterns[0], pattern);
  }
  for (const pattern of ['', 'model?', 'model[ab]']) {
    assert.throws(() => parseReasoningRules([rule([pattern], hidden)]), /INVALID_REASONING_CAPABILITY/);
  }
});

test('loaded capabilities normalize only the draft and never mutate the saved model', () => {
  const saved = {
    model_name: 'glm-5.2',
    model_provider: 'OpenAI',
    api_base: vendor.api_base,
    api_key: 'test',
    vendor_key: 'baidu',
    plan: 'custom_api',
    reasoning_level: 'high',
  };
  const draft = createModelDraft(saved, catalog);
  assert.equal(draft.reasoning_level, 'high');
  assert.equal(reconcileModelReasoning(draft, catalog).reasoning_level, '');
  assert.equal(saved.reasoning_level, 'high');
  assert.equal(reconcileModelReasoning({ ...draft, reasoning_level: 'on' }, catalog).reasoning_level, 'on');
  const noReasoning = reconcileModelReasoning({ ...draft, model_name: 'codegeex-4' }, catalog);
  assert.equal(noReasoning.reasoning_level, '');
  const entry = modelDraftToEntry(noReasoning, saved, catalog, true);
  assert.equal(entry.reasoning_level, '');
  for (const field of ['reasoning', 'thinking', 'reasoning_effort', 'enable_thinking']) {
    assert.equal(field in entry, false);
  }
  assert.equal(reconcileModelReasoning(draft, { ...catalog, reasoning: null }).reasoning_level, 'high');
});

let createRoot;
let ModelDialog;
let SettingsServicesProvider;
let I18nextProvider;
let dom;
const previousGlobals = new Map();
const i18n = i18next.createInstance();

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
  // jsdom lacks native dialog opening/closing; the actual dialog and form still render.
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
        export { ModelDialog } from './src/features/settings/modules/models/ModelDialog';
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
    // Only Vite's static icon discovery is stubbed; model logic and controls are real.
    define: { 'import.meta.glob': 'testAssetGlob' },
    banner: { js: 'const testAssetGlob = () => ({});' },
  });
  const compiled = { exports: {} };
  const require = createRequire(import.meta.url);
  new Function('require', 'module', 'exports', result.outputFiles[0].text)(require, compiled, compiled.exports);
  ({ ModelDialog, SettingsServicesProvider } = compiled.exports);
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

async function mountDialog(t, modelOverrides = {}, propOverrides = {}) {
  const model = Object.freeze({
    model_name: 'glm-5.2',
    model_provider: 'OpenAI',
    api_base: vendor.api_base,
    api_key: 'test-key',
    vendor_key: 'baidu',
    plan: 'custom_api',
    reasoning_level: 'high',
    ...modelOverrides,
  });
  const requests = [];
  const saved = [];
  let closed = 0;
  let props = {
    model,
    models: [],
    catalog,
    catalogLoading: false,
    catalogError: '',
    saving: false,
    onClose: () => {
      closed += 1;
    },
    onSave: async (entry) => {
      saved.push(entry);
    },
    onRetryCatalog: () => undefined,
    ...propOverrides,
  };
  const request = async (method, params) => {
    requests.push({ method, params });
    if (method === 'config.validate_model') return {};
    if (method === 'openai_account.auth.pending_login') return { status: 'none', auth: { authenticated: true } };
    if (method === 'openai_account.models.list') return { models: ['glm-5.2'], auth: { authenticated: true } };
    throw new Error(`Unexpected request: ${method}`);
  };
  const root = createRoot(document.getElementById('root'));
  t.after(async () => {
    await act(async () => root.unmount());
  });
  async function render(patch = {}) {
    props = { ...props, ...patch };
    await act(async () =>
      root.render(
        createElement(
          I18nextProvider,
          { i18n },
          createElement(
            SettingsServicesProvider,
            { isConnected: true, connectionState: 'connected', request },
            createElement(ModelDialog, props),
          ),
        ),
      ),
    );
  }
  await render();
  return { model, requests, saved, render, closed: () => closed };
}

function reasoningSelect() {
  const label = [...document.querySelectorAll('.settings-model-dialog label')].find(
    (node) => node.textContent.trim() === i18n.t('settingsPanel.fields.reasoning_level.title'),
  );
  return label ? document.getElementById(label.htmlFor) : null;
}

function dialogButton(key, dialog = document.querySelector('.settings-model-dialog')) {
  const button = [...dialog.querySelectorAll('button')].find((node) => node.textContent.trim() === i18n.t(key));
  assert.ok(button, `Button not found: ${key}`);
  return button;
}

async function click(element) {
  assert.ok(element);
  await act(async () => element.click());
}

async function selectValue(element, value) {
  await act(async () => {
    element.value = value;
    element.dispatchEvent(new dom.window.Event('change', { bubbles: true }));
  });
}

test('opening an unsupported saved value displays and submits default without changing the saved configuration', async (t) => {
  const ui = await mountDialog(t);
  assert.deepEqual(
    [...reasoningSelect().options].map(({ value }) => value),
    ['', 'off', 'on'],
  );
  assert.equal(reasoningSelect().value, '');
  assert.equal(ui.model.reasoning_level, 'high');
  assert.equal(ui.saved.length, 0);
  await click(dialogButton('common.confirm'));
  assert.equal(ui.requests.find(({ method }) => method === 'config.validate_model').params.reasoning_level, '');
  assert.equal(ui.saved[0].reasoning_level, '');
  assert.equal(ui.model.reasoning_level, 'high');
});

test('catalog loading or failure preserves the old value; a successful response normalizes only the draft', async (t) => {
  const unavailable = { reasoning: null, token_plan: [], coding_plan: [], custom_api: [] };
  const ui = await mountDialog(t, {}, { catalog: unavailable, catalogLoading: true });
  assert.equal(reasoningSelect(), null);
  assert.equal(dialogButton('common.confirm').disabled, true);
  await ui.render({ catalogLoading: false, catalogError: 'Catalog unavailable' });
  assert.equal(dialogButton('common.confirm').disabled, true);
  assert.equal(ui.model.reasoning_level, 'high');
  assert.equal(ui.saved.length, 0);
  await ui.render({
    catalog: {
      ...catalog,
      custom_api: [{ ...preset, reasoning_capabilities: { 'glm-5.2': both(strengths) } }],
    },
    catalogError: '',
  });
  assert.equal(reasoningSelect().value, 'high', 'a failed load must not clear the draft');
  await ui.render({ catalog });
  assert.equal(reasoningSelect().value, '');
  assert.equal(dialogButton('common.confirm').disabled, false);
  await click(dialogButton('common.cancel'));
  const discardDialog = [...document.querySelectorAll('dialog[open]')].find((node) =>
    node.textContent.includes(i18n.t('settingsPanel.dialog.discardTitle')),
  );
  await click(dialogButton('common.confirm', discardDialog));
  assert.equal(ui.closed(), 1);
  assert.equal(ui.saved.length, 0);
  assert.equal(ui.model.reasoning_level, 'high');
});

test('model and protocol changes retain supported values and hide empty capabilities', async (t) => {
  const ui = await mountDialog(t, { reasoning_level: 'on' });
  assert.equal(reasoningSelect().value, 'on');
  const protocol = document.querySelector('.settings-model-dialog select');
  await selectValue(protocol, 'anthropic');
  assert.equal(reasoningSelect().value, 'on');
  await click(document.querySelector('.settings-model-name-field__toggle'));
  const modelOption = [...document.querySelectorAll('[role="option"]')].find(
    (node) => node.textContent.trim() === 'codegeex-4',
  );
  await click(modelOption);
  assert.equal(reasoningSelect(), null);
  await click(dialogButton('common.confirm'));
  assert.equal(ui.saved[0].reasoning_level, '');
  assert.equal(ui.saved[0].model_name, 'codegeex-4');
});

test('switching protocol or provider resets a selection only when the new options exclude it', async (t) => {
  const protocolCatalog = {
    ...catalog,
    custom_api: [{ ...preset, reasoning_capabilities: { 'glm-5.2': { openai: toggle, anthropic: strengths } } }],
  };
  const ui = await mountDialog(t, { model_provider: 'Anthropic' }, { catalog: protocolCatalog });
  assert.equal(reasoningSelect().value, 'high');
  await selectValue(document.querySelector('.settings-model-dialog select'), 'openai');
  assert.equal(reasoningSelect().value, '');
  await selectValue(reasoningSelect(), 'on');
  const anotherPreset = {
    ...preset,
    vendor_key: 'deepseek',
    default_model: 'deepseek-v4-pro',
    model_options: ['deepseek-v4-pro'],
    reasoning_capabilities: { 'deepseek-v4-pro': both(strengths) },
  };
  await ui.render({ catalog: { ...protocolCatalog, custom_api: [...protocolCatalog.custom_api, anotherPreset] } });
  assert.equal(reasoningSelect().value, 'on');
  await click(document.querySelector('.settings-model-provider-select__trigger'));
  await click(
    [...document.querySelectorAll('[role="option"]')].find(
      (node) => node.textContent.trim() === i18n.t('settingsPanel.models.vendors.deepseek'),
    ),
  );
  assert.equal(document.querySelector('input[role="combobox"]').value, 'deepseek-v4-pro');
  assert.deepEqual(
    [...reasoningSelect().options].map(({ value }) => value),
    ['', 'off', 'high', 'max'],
  );
  assert.equal(reasoningSelect().value, '');
});

test('account refresh preserves a supported selection and uses the same draft normalization', async (t) => {
  const ui = await mountDialog(t, { model_provider: 'OpenAIAccount', vendor_key: undefined, plan: undefined });
  assert.equal(reasoningSelect().value, 'high');
  await selectValue(reasoningSelect(), 'max');
  await click(document.querySelector('.settings-model-name-field__refresh'));
  assert.equal(reasoningSelect().value, 'max');
  await click(dialogButton('common.confirm'));
  assert.equal(ui.saved[0].reasoning_level, 'max');
});
