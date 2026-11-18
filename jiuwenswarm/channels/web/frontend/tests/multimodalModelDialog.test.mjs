import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';

const i18n = i18next.createInstance();
const previousGlobals = new Map();
let dom;
let createRoot;
let MultimodalModelDialog;
let MultimodalSettingsFeedback;
let SettingsServicesProvider;
let I18nextProvider;
let MultimodalSettingsStore;
let SettingsSaveQueue;

before(async () => {
  dom = new JSDOM('<!doctype html><div id="root"></div>', { pretendToBeVisual: true });
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
        export { MultimodalModelDialog } from './src/features/settings/modules/models/MultimodalModelDialog';
        export { MultimodalSettingsFeedback } from './src/features/settings/modules/models/MultimodalSettingsFeedback';
        export { SettingsServicesProvider } from './src/features/settings/services/SettingsServicesProvider';
        export { MultimodalSettingsStore } from './src/features/settings/services/multimodalSettings';
        export { SettingsSaveQueue } from './src/features/settings/services/SettingsSaveQueue';
      `,
      resolveDir: fileURLToPath(new URL('../', import.meta.url)), loader: 'ts',
    },
    bundle: true, write: false, platform: 'node', format: 'cjs', packages: 'external',
    loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
    define: { 'import.meta.glob': 'testAssetGlob', 'import.meta.env': '{}' },
    banner: { js: 'const testAssetGlob = () => ({});' },
  });
  const compiled = { exports: {} };
  const require = createRequire(import.meta.url);
  new Function('require', 'module', 'exports', result.outputFiles[0].text)(require, compiled, compiled.exports);
  ({ MultimodalModelDialog, MultimodalSettingsFeedback, SettingsServicesProvider,
    MultimodalSettingsStore, SettingsSaveQueue } = compiled.exports);
  ({ I18nextProvider } = require('react-i18next'));
  await i18n.init({
    lng: 'zh', showSupportNotice: false,
    resources: Object.fromEntries(['zh', 'en'].map((locale) => [locale, {
      translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${locale}.json`, import.meta.url), 'utf8')),
    }])),
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

async function mountDialog(t, purpose, language, options = {}) {
  await i18n.changeLanguage(language);
  const writes = [];
  let closed = 0;
  let finish;
  const request = async (method, params) => {
    if (method === 'vendors.list') return { vendors: {
      token_plan: [], coding_plan: [], custom_api: options.preset ? [options.preset] : [],
      reasoning: {
        protocol_defaults: {
          openai: { options: [], recommended: null }, anthropic: { options: [], recommended: null },
        },
        model_fallbacks: [],
      },
    } };
    if (method === 'multimodal.models.list') return { schema_version: 1, models: structuredClone(options.models ?? []), tools: {} };
    assert.equal(method, 'multimodal.models.upsert');
    writes.push(params);
    if (options.error) {
      if (options.deferred) await new Promise((resolve) => { finish = resolve; });
      const error = typeof options.error === 'function' ? options.error(writes.length) : options.error;
      if (error) throw error;
    }
    return {
      saved: true, changed: true, applied_without_restart: true,
      snapshot: { schema_version: 1, models: [], tools: {} },
    };
  };
  const root = createRoot(document.getElementById('root'));
  t.after(async () => { await act(async () => root.unmount()); });
  await act(async () => root.render(
    createElement(I18nextProvider, { i18n },
      createElement(SettingsServicesProvider, { isConnected: true, connectionState: 'connected', request },
        createElement(MultimodalSettingsFeedback),
        createElement(MultimodalModelDialog, {
          defaultCapability: options.add ? (purpose === 'generation' ? 'visual_gen' : 'vision') : undefined,
          enableToolOnSave: options.enableToolOnSave,
          model: options.add ? undefined : {
            model_id: 'mm_test', display_name: options.displayName ?? 'Test model', purpose, capabilities: [],
            connection: {
              provider: 'OpenAI', protocol: 'openai', api_base: 'https://model.example/v1',
              api_key: 'test-key', model_name: 'test-model', context_window_tokens: 32000, ...options.connection,
            },
          },
          onClose: () => { closed += 1; },
        }),
      ),
    ),
  ));
  return { writes, closed: () => closed, finish: () => finish() };
}

function saveButton(purpose) {
  return get(`settings-agent-${purpose === 'understanding' ? 'media-' : ''}config-dialog-save-btn`);
}

async function selectCapability(capability) {
  await act(async () => get('settings-multimodal-capabilities-select').click());
  await act(async () => document.querySelector(`[role="option"][data-variant="${capability}"]`).click());
  await act(async () => document.querySelector('[role="listbox"]').dispatchEvent(
    new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }),
  ));
}

for (const purpose of ['understanding', 'generation']) {
  for (const language of ['zh', 'en']) {
    test(`${purpose} shows the ${language} capability error inline and saves after selection`, async (t) => {
      const state = await mountDialog(t, purpose, language);
      await act(async () => saveButton(purpose).click());
      const field = document.querySelector('[data-testid="settings-multimodal-metadata-field"][data-variant="capabilities"]');
      assert.equal(field.querySelector('[role="alert"]')?.textContent, i18n.t('settingsPanel.multimodal.selectCapabilities'));
      assert.equal(document.querySelector('.settings-page__error'), null);
      assert.equal(state.writes.length, 0);
      assert.equal(state.closed(), 0);
      const capability = purpose === 'generation' ? 'visual_gen' : 'vision';
      await selectCapability(capability);
      assert.equal(field.querySelector('[role="alert"]'), null);
      await act(async () => saveButton(purpose).click());
      assert.equal(state.writes.length, 1);
      assert.deepEqual(state.writes[0].model.capabilities, [capability]);
      assert.equal(state.closed(), 1);
    });
  }

  test(`${purpose} still displays actual save failures`, async (t) => {
    const state = await mountDialog(t, purpose, 'zh', { error: new Error('Save failed') });
    await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
    await act(async () => saveButton(purpose).click());
    assert.equal(state.writes.length, 1);
    assert.ok(document.querySelector('.settings-page__error[role="alert"]'));
    assert.equal(state.closed(), 0);
  });
}

async function selectPurpose(purpose) {
  const purposeInput = document.querySelector(
    '[data-variant="purpose"] [data-testid="settings-multimodal-metadata-field-input"]',
  );
  await act(async () => purposeInput.click());
  await act(async () => document.querySelector(`[role="option"][data-variant="${purpose}"]`).click());
}

async function fillConnection(purpose, prefix) {
  await act(async () => get('settings-model-provider-select-trigger').click());
  await act(async () => document.querySelector('[data-testid="settings-model-provider-select-option"][data-variant="custom"]').click());
  if (purpose === 'generation') {
    await act(async () => fieldNode('protocol').querySelector('button').click());
    await act(async () => document.querySelector('[role="option"][data-variant="openai"]').click());
  }
  await inputField('api_base', `https://${prefix}.example/v1`);
  await inputField('api_key', `${prefix}-key`);
  await inputField('model_name', `${prefix}-model`);
  await act(async () => fieldNode('model_name').querySelector('input').dispatchEvent(
    new dom.window.FocusEvent('focusout', { bubbles: true }),
  ));
}

function assertResetFields(purpose) {
  assert.equal(fieldNode('display_name').querySelector('input').value, '');
  assert.equal(fieldNode('api_key').querySelector('input').value, '');
  assert.equal(fieldNode('model_name').querySelector('input').value, '');
  assert.equal(fieldNode('model_name').querySelector('input').disabled, true);
  assert.equal(fieldNode('api_base'), null);
  assert.equal(document.querySelector('[role="alert"]'), null);
  if (purpose === 'understanding') {
    assert.equal(fieldNode('context_window_tokens').querySelector('input').value, '256K');
  } else assert.equal(fieldNode('context_window_tokens'), null);
}

for (const purpose of ['understanding', 'generation']) {
  const nextPurpose = purpose === 'understanding' ? 'generation' : 'understanding';
  for (const add of [false, true]) {
    test(`${purpose} ${add ? 'create' : 'edit'} resets all fields and validation when changing purpose`, async (t) => {
      const state = await mountDialog(t, purpose, 'zh', { add });
      if (!add) await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
      await fillConnection(purpose, 'previous');
      if (purpose === 'understanding') await inputField('context_window_tokens', '1M');
      await inputField('display_name', 'x'.repeat(101));
      await act(async () => saveButton(purpose).click());
      assert.ok(fieldNode('display_name').querySelector('[role="alert"]'));
      await selectPurpose(nextPurpose);
      assertResetFields(nextPurpose);
      await act(async () => saveButton(nextPurpose).click());
      assert.equal(fieldNode('capabilities').querySelector('[role="alert"]')?.textContent, '请选择模型能力');
      assert.equal(state.writes.length, 0);
      assert.equal(document.querySelector('.settings-page__error'), null);
      const capability = nextPurpose === 'generation' ? 'visual_gen' : 'vision';
      await selectCapability(capability);
      await fillConnection(nextPurpose, 'next');
      assert.equal(fieldNode('display_name').querySelector('input').value, 'next-model');
      await act(async () => saveButton(nextPurpose).click());
      assert.equal(state.writes.length, 1);
      assert.equal(state.writes[0].mode, add ? 'create' : 'update');
      if (!add) assert.equal(state.writes[0].model.model_id, 'mm_test');
      assert.equal(state.writes[0].model.purpose, nextPurpose);
      assert.deepEqual(state.writes[0].model.capabilities, [capability]);
      assert.equal(state.writes[0].model.display_name, 'next-model');
      assert.equal(state.writes[0].model.connection.api_base, 'https://next.example/v1');
      assert.equal(state.writes[0].model.connection.api_key, 'next-key');
      assert.equal(state.writes[0].model.connection.model_name, 'next-model');
      assert.equal(state.closed(), 1);
    });
  }

  test(`${purpose} switching back stays empty and preserves the unsaved-changes confirmation`, async (t) => {
    const state = await mountDialog(t, purpose, 'zh');
    await selectPurpose(nextPurpose);
    await selectCapability(nextPurpose === 'generation' ? 'visual_gen' : 'vision');
    await fillConnection(nextPurpose, 'temporary');
    await selectPurpose(purpose);
    assertResetFields(purpose);
    assert.equal(state.writes.length, 0);
    await act(async () => get(`settings-agent-${purpose === 'understanding' ? 'media-' : ''}config-dialog-cancel-btn`).click());
    assert.ok([...document.querySelectorAll('dialog[open] .settings-confirm-dialog__message')]
      .some((node) => node.textContent === i18n.t('settingsPanel.dialog.discardConfirm')));
    assert.equal(state.closed(), 0);
  });
}

function fieldNode(name) {
  return document.querySelector(`[data-testid$="-field"][data-variant="${name}"]`);
}

async function inputField(name, value) {
  const input = fieldNode(name).querySelector('input');
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(input, value);
    input.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
}

for (const purpose of ['understanding', 'generation']) {
  for (const language of ['zh', 'en']) {
    for (const add of [false, true]) {
      test(`${purpose} ${add ? 'create' : 'edit'} rejects duplicate display names across purposes in ${language}`, async (t) => {
        const state = await mountDialog(t, purpose, language, {
          add,
          displayName: 'Shared name',
          models: [
            { model_id: 'mm_other', display_name: ' Shared name ', purpose: purpose === 'generation' ? 'understanding' : 'generation' },
            ...(!add ? [{ model_id: 'mm_test', display_name: 'Shared name', purpose }] : []),
          ],
        });
        if (add) {
          await act(async () => get('settings-model-provider-select-trigger').click());
          await act(async () => document.querySelector('[data-testid="settings-model-provider-select-option"][data-variant="custom"]').click());
          if (purpose === 'generation') {
            await act(async () => fieldNode('protocol').querySelector('button').click());
            await act(async () => document.querySelector('[role="option"][data-variant="openai"]').click());
          }
          await inputField('api_base', 'https://model.example/v1');
          await inputField('api_key', 'test-key');
          await inputField('model_name', 'test-model');
          if (purpose === 'understanding') await inputField('context_window_tokens', '32000');
        } else {
          await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
        }
        await inputField('display_name', '  Shared name  ');
        await act(async () => saveButton(purpose).click());
        const field = fieldNode('display_name');
        assert.equal(field.querySelector('[role="alert"]')?.textContent, language === 'zh'
          ? '模型展示名称必须全局唯一，不能与其他模型的展示名称重复'
          : 'Model display name must be globally unique and cannot match another model\'s display name');
        assert.equal(document.querySelectorAll('[role="alert"]').length, 1);
        assert.equal(document.querySelector('.settings-page__error'), null);
        assert.equal(state.writes.length, 0);
        assert.equal(state.closed(), 0);
        await inputField('display_name', '  Unique name  ');
        assert.equal(field.querySelector('[role="alert"]'), null);
        await act(async () => saveButton(purpose).click());
        assert.equal(state.writes.length, 1);
        assert.equal(state.writes[0].mode, add ? 'create' : 'update');
        assert.equal(state.writes[0].model.display_name, 'Unique name');
        assert.equal(state.closed(), 1);
      });
    }
  }

  test(`${purpose} display-name validation excludes itself and compares case sensitively`, async (t) => {
    const state = await mountDialog(t, purpose, 'zh', { models: [
      { model_id: 'mm_test', display_name: 'Test model', purpose },
      { model_id: 'mm_other', display_name: 'TEST MODEL', purpose },
    ] });
    await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
    await act(async () => saveButton(purpose).click());
    assert.equal(state.writes.length, 1);
    assert.equal(state.writes[0].model.model_id, 'mm_test');
    assert.equal(document.querySelector('[role="alert"]'), null);
  });

  test(`${purpose} display-name validation uses the refreshed catalog while the dialog stays open`, async (t) => {
    const models = [];
    const state = await mountDialog(t, purpose, 'zh', { models });
    await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
    models.push({ model_id: 'mm_other', display_name: 'Test model', purpose });
    await act(async () => window.dispatchEvent(new dom.window.Event('jiuwen:settings-config-changed')));
    await act(async () => saveButton(purpose).click());
    assert.equal(fieldNode('display_name').querySelector('[role="alert"]')?.textContent,
      '模型展示名称必须全局唯一，不能与其他模型的展示名称重复');
    assert.equal(state.writes.length, 0);
  });
}

function validationError(field) {
  return Object.assign(new Error('validation failed'), {
    payload: { code: 'MM_VALIDATION_FAILED', ...(field ? { field_errors: [{ field, key: 'invalid' }] } : {}) },
  });
}

function assertServerValidation(purpose, language = 'zh') {
  const error = get(`settings-agent-${purpose === 'understanding' ? 'media-' : ''}config-dialog-error`);
  assert.equal(error.textContent, language === 'zh'
    ? '模型配置校验失败，请检查后重试。'
    : 'Model configuration validation failed. Check the configuration and try again.');
  assert.equal(document.querySelectorAll('[role="alert"]').length, 1);
  assert.equal(document.querySelector('.form-item__error'), null);
  assert.equal(document.querySelector('[data-testid="settings-multimodal-feedback"]'), null);
}

for (const purpose of ['understanding', 'generation']) {
  const localCases = {
    api_base: ['', 'ftp://model.example', 'https://' + 'x'.repeat(505)],
    api_key: ['', 'x'.repeat(2049)],
    model_name: ['', 'x'.repeat(101)],
    display_name: ['', 'x'.repeat(101)],
    ...(purpose === 'understanding' ? { context_window_tokens: ['', '0', '1.5', '-1', 'abc'] } : {}),
  };
  for (const [field, values] of Object.entries(localCases)) {
    for (const [index, value] of values.entries()) {
      test(`${purpose} local ${field} validation ${index} appears only below its field`, async (t) => {
        const state = await mountDialog(t, purpose, 'zh');
        await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
        await inputField(field, value);
        await act(async () => saveButton(purpose).click());
        assert.ok(fieldNode(field).querySelector('[role="alert"]'));
        assert.equal(document.querySelector('.settings-page__error'), null);
        assert.equal(state.writes.length, 0);
      });
    }
  }

  test(`${purpose} add dialog shows missing provider and protocol errors below their fields`, async (t) => {
    const state = await mountDialog(t, purpose, 'zh', { add: true });
    await act(async () => saveButton(purpose).click());
    assert.ok(fieldNode('vendor_selection').querySelector('[role="alert"]'));
    if (purpose === 'generation') assert.ok(fieldNode('protocol').querySelector('[role="alert"]'));
    assert.equal(document.querySelector('.settings-page__error'), null);
    assert.equal(state.writes.length, 0);
  });

  for (const language of ['zh', 'en']) {
    for (const field of ['display_name', 'model', undefined]) {
      test(`${purpose} server validation with ${field ?? 'no field details'} appears only in the ${language} dialog footer`, async (t) => {
        const state = await mountDialog(t, purpose, language, { error: validationError(field), deferred: true });
        await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
        await act(async () => saveButton(purpose).click());
        assert.equal(state.writes.length, 1);
        assert.equal(saveButton(purpose).disabled, true);
        await act(async () => state.finish());
        assertServerValidation(purpose, language);
        assert.equal(fieldNode('display_name').querySelector('input').value, 'Test model');
        assert.equal(state.closed(), 0);
        assert.equal(saveButton(purpose).disabled, false);
      });
    }
  }

  test(`${purpose} shows server validation only in the dialog footer after reference confirmation`, async (t) => {
    const error = (attempt) => attempt === 1 ? Object.assign(new Error('confirm'), {
      payload: { code: 'MM_CONFIRM_REQUIRED', affected_tools: ['vision'] },
    }) : validationError('connection.api_key');
    const state = await mountDialog(t, purpose, 'zh', { error, deferred: true });
    await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
    await act(async () => saveButton(purpose).click());
    await act(async () => state.finish());
    const confirm = [...document.querySelectorAll('.settings-confirm-dialog__footer button')]
      .find((button) => button.textContent === i18n.t('settingsPanel.multimodal.confirmChanges'));
    assert.ok(confirm);
    await act(async () => confirm.click());
    assert.equal(state.writes.length, 2);
    await act(async () => state.finish());
    assertServerValidation(purpose);
    assert.equal(state.closed(), 0);
  });

  test(`${purpose} clears server validation on retry and saves the edited configuration`, async (t) => {
    const error = (attempt) => attempt === 1 ? validationError('display_name') : null;
    const state = await mountDialog(t, purpose, 'zh', { error, deferred: true });
    await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
    await act(async () => saveButton(purpose).click());
    await act(async () => state.finish());
    assertServerValidation(purpose);
    await inputField('display_name', 'updated-value');
    await act(async () => saveButton(purpose).click());
    assert.equal(document.querySelector('[role="alert"]'), null);
    await act(async () => state.finish());
    assert.equal(state.writes.length, 2);
    assert.equal(state.writes[1].model.display_name, 'updated-value');
    assert.equal(state.closed(), 1);
    assert.equal(document.querySelector('.settings-page__error'), null);
  });

  test(`${purpose} cancels reference confirmation without reporting a save failure`, async (t) => {
    const error = Object.assign(new Error('confirm'), {
      payload: { code: 'MM_CONFIRM_REQUIRED', affected_tools: ['vision'] },
    });
    const state = await mountDialog(t, purpose, 'zh', { error });
    await selectCapability(purpose === 'generation' ? 'visual_gen' : 'vision');
    await act(async () => saveButton(purpose).click());
    const cancel = [...document.querySelectorAll('dialog[open] .settings-confirm-dialog__footer button')]
      .find((button) => button.textContent === i18n.t('common.cancel'));
    assert.ok(cancel);
    await act(async () => cancel.click());
    assert.equal(state.writes.length, 1);
    assert.equal(state.closed(), 0);
    assert.equal(saveButton(purpose).disabled, false);
    assert.equal(document.querySelector('[role="alert"]'), null);
  });
}

test('vLLM-Omni retains optional API key and model name', async (t) => {
  const state = await mountDialog(t, 'generation', 'zh', {
    connection: { protocol: 'vllm-omni', api_key: '', model_name: '' },
  });
  await selectCapability('visual_gen');
  await act(async () => saveButton('generation').click());
  assert.equal(state.writes.length, 1);
  assert.equal(state.closed(), 1);
  assert.equal(document.querySelector('[role="alert"]'), null);
});

test('an immediate server validation response also appears only in the dialog footer', async (t) => {
  const state = await mountDialog(t, 'generation', 'zh', { error: validationError('display_name') });
  await selectCapability('visual_gen');
  await act(async () => saveButton('generation').click());
  assert.equal(state.writes.length, 1);
  assertServerValidation('generation');
});

function activationError(activationTools, affectedTools = []) {
  return Object.assign(new Error('confirmation required'), {
    payload: { code: 'MM_CONFIRM_REQUIRED', affected_tools: affectedTools, activation_tools: activationTools },
  });
}

test('saving the original capability from a tool switch sends its activation with the model', async (t) => {
  const state = await mountDialog(t, 'understanding', 'zh', {
    add: true,
    enableToolOnSave: true,
    error: (attempt) => attempt === 1 && state.writes[0].tool_activation?.enabled !== true
      ? activationError(['vision']) : null,
  });
  await fillConnection('understanding', 'switch');
  await act(async () => saveButton('understanding').click());
  assert.equal(state.writes.length, 1);
  assert.deepEqual(state.writes[0].model.capabilities, ['vision']);
  assert.deepEqual(state.writes[0].tool_activation, { tools: ['vision'], enabled: true });
  assert.equal(document.querySelector('[data-testid="settings-multimodal-activation-tools"]'), null);
  assert.equal(state.closed(), 1);
});

function confirmationButton(key) {
  const button = [...document.querySelectorAll('dialog[open] .settings-confirm-dialog__footer button')]
    .find((node) => node.textContent === i18n.t(key));
  assert.ok(button, `Missing confirmation button ${key}`);
  return button;
}

for (const purpose of ['understanding', 'generation']) {
  for (const language of ['zh', 'en']) {
    for (const enabled of [false, true]) {
      test(`${purpose} ${language} confirms all disabled tools together with enabled=${enabled}`, async (t) => {
        const tools = purpose === 'understanding' ? ['vision', 'audio'] : ['visual_gen', 'video_gen'];
        const state = await mountDialog(t, purpose, language, {
          error: (attempt) => attempt === 1 ? activationError(tools) : null,
        });
        for (const tool of tools) await selectCapability(tool);
        await act(async () => saveButton(purpose).click());
        assert.equal(state.writes.length, 1);
        assert.equal(state.writes[0].tool_activation, undefined);
        assert.equal(state.closed(), 0);
        assert.equal(saveButton(purpose).disabled, true);
        assert.equal(document.querySelectorAll('dialog[open] .settings-confirm-dialog').length, 1);
        assert.equal(document.querySelectorAll('.settings-confirm-dialog input[type="checkbox"]').length, 0);
        const message = get('settings-multimodal-activation-tools').textContent;
        for (const tool of tools) assert.ok(message.includes(i18n.t(`settingsPanel.multimodal.tools.${tool}`)));
        assert.ok(message.includes('Test model'));
        await act(async () => confirmationButton(
          `settingsPanel.multimodal.${enabled ? 'enableAndUse' : 'keepDisabled'}`,
        ).click());
        assert.equal(state.writes.length, 2);
        assert.deepEqual(state.writes[1].tool_activation, { tools, enabled });
        assert.equal(state.closed(), 1);
        assert.equal(document.querySelector('[role="alert"]'), null);
      });
    }
  }

  test(`${purpose} closing the activation dialog cancels saving and retains the draft`, async (t) => {
    const tool = purpose === 'understanding' ? 'vision' : 'visual_gen';
    const state = await mountDialog(t, purpose, 'zh', { error: activationError([tool]) });
    await selectCapability(tool);
    await act(async () => saveButton(purpose).click());
    await act(async () => document.querySelector('dialog[open] .settings-confirm-dialog__close').click());
    assert.equal(state.writes.length, 1);
    assert.equal(state.closed(), 0);
    assert.equal(saveButton(purpose).disabled, false);
    assert.equal(fieldNode('display_name').querySelector('input').value, 'Test model');
    assert.equal(document.querySelector('[role="alert"]'), null);
  });
}

test('changed activation targets require a new user choice instead of replaying the old choice', async (t) => {
  const state = await mountDialog(t, 'understanding', 'zh', {
    error: (attempt) => attempt < 3 ? activationError(attempt === 1 ? ['vision', 'audio'] : ['audio']) : null,
  });
  await selectCapability('vision');
  await selectCapability('audio');
  await act(async () => saveButton('understanding').click());
  await act(async () => confirmationButton('settingsPanel.multimodal.enableAndUse').click());
  assert.equal(state.writes.length, 2);
  assert.equal(state.closed(), 0);
  assert.ok(!get('settings-multimodal-activation-tools').textContent.includes(i18n.t('settingsPanel.multimodal.tools.vision')));
  await act(async () => confirmationButton('settingsPanel.multimodal.keepDisabled').click());
  assert.equal(state.writes.length, 3);
  assert.deepEqual(state.writes[1].tool_activation, { tools: ['vision', 'audio'], enabled: true });
  assert.deepEqual(state.writes[2].tool_activation, { tools: ['audio'], enabled: false });
  assert.equal(state.closed(), 1);
});

test('one dialog explains both removed references and new disabled capabilities', async (t) => {
  const state = await mountDialog(t, 'generation', 'en', {
    error: (attempt) => attempt === 1 ? activationError(['visual_gen', 'video_gen'], ['vision']) : null,
  });
  await selectCapability('visual_gen');
  await selectCapability('video_gen');
  await act(async () => saveButton('generation').click());
  assert.ok(get('settings-multimodal-reference-tools').textContent.includes(i18n.t('settingsPanel.multimodal.tools.vision')));
  assert.ok(get('settings-multimodal-activation-tools').textContent.includes(i18n.t('settingsPanel.multimodal.tools.video_gen')));
  assert.equal(document.querySelectorAll('dialog[open] .settings-confirm-dialog').length, 1);
  await act(async () => confirmationButton('settingsPanel.multimodal.keepDisabled').click());
  assert.deepEqual(state.writes[1].confirmed_tools, ['vision']);
  assert.deepEqual(state.writes[1].tool_activation, { tools: ['visual_gen', 'video_gen'], enabled: false });
  assert.equal(state.closed(), 1);
});

for (const operation of ['models.upsert', 'tools.update', 'models.delete', 'config.apply']) {
  test(`${operation} keeps server validation owned by its actual caller`, async () => {
    const error = validationError('connection.api_key');
    const store = new MultimodalSettingsStore(async () => { throw error; }, new SettingsSaveQueue());
    await assert.rejects(store.write(operation, {}), (actual) => actual === error);
    assert.equal(store.getSnapshot().saving, false);
    if (operation === 'models.upsert') assert.equal(store.getSnapshot().error, null);
    else assert.equal(store.getSnapshot().error.cause, error);
  });
}
