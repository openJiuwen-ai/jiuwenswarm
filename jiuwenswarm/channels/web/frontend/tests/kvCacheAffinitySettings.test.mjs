import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

await build({
  stdin: {
    contents: `export * from './src/features/settings/modules/models/modelAdapters';
      export { ModelDialog } from './src/features/settings/modules/models/ModelDialog';
      export { parseModelResource, buildModelResourcePayload } from './src/features/settings/modules/models/ModelsSettings';
      export { ToastStack, toast } from './src/components/ui';
      export { useSettingsConfig } from './src/features/settings/services/useSettingsConfig';
      export { experimentalModule } from './src/features/settings/modules/experimental/definition';
      export { SettingsPageLayout } from './src/features/settings/SettingsPageLayout';
      export { SettingsServicesProvider } from './src/features/settings/services/SettingsServicesProvider';`,
    resolveDir: process.cwd(), loader: 'tsx',
  },
  bundle: true, packages: 'external', platform: 'node', format: 'esm',
  outfile: 'node_modules/.cache/affinity-settings/ui.mjs',
  define: { 'import.meta.env': '{}', 'import.meta.glob': 'testAssetGlob' },
  banner: { js: 'const testAssetGlob = () => ({});' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl', '.webp': 'dataurl' },
  plugins: [{ name: 'source-and-icons', setup(builder) {
    builder.onResolve({ filter: /\.svg\?react$/ }, (args) => ({ path: args.path, namespace: 'icon' }));
    builder.onLoad({ filter: /.*/, namespace: 'icon' }, () => ({ contents: 'export default () => null;' }));
  } }],
});

const ui = await import('../node_modules/.cache/affinity-settings/ui.mjs');
const resources = Object.fromEntries(['en', 'zh'].map((lng) => [lng, {
  translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${lng}.json`, import.meta.url), 'utf8')),
}]));
await i18next.use(initReactI18next).init({ lng: 'en', showSupportNotice: false, resources });
const catalog = { token_plan: [], coding_plan: [], custom_api: [], reasoning: {
  protocol_defaults: { openai: { options: [], recommended: null }, anthropic: { options: [], recommended: null } },
  model_fallbacks: [],
} };
const model = {
  model_id: 'mdl_local', alias: 'Local', model_name: 'local', api_key: 'test', api_base: 'http://service/v1',
  model_provider: 'OpenAI', reasoning_level: '', is_default: true,
  extensions: { kv_cache: { mode: 'affinity', affinity_field: 'agent_hint' }, thinking: { mode: 'auto' } },
};

async function mount(t, child, source = {}, request = async () => ({})) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
  dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
  const previous = new Map();
  for (const [key, value] of Object.entries({ window: dom.window, document: dom.window.document,
    localStorage: dom.window.localStorage, sessionStorage: dom.window.sessionStorage,
    IS_REACT_ACT_ENVIRONMENT: true, affinitySource: { savingKeys: new Set(), save: async () => {}, ...source },
  })) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const root = createRoot(dom.window.document.getElementById('root'));
  const render = async (next) => act(async () => root.render(createElement(ui.SettingsServicesProvider, {
    request, isConnected: true, connectionState: 'connected',
  }, [createElement(ui.ToastStack, { key: 'toasts' }), createElement('div', { key: 'content' }, next)])));
  await render(child);
  t.after(async () => {
    await act(async () => {
      root.unmount();
      await new Promise((resolve) => setTimeout(resolve, 250));
    });
    dom.window.close();
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  });
  return { document: dom.window.document, render };
}

test('model resource declaration round-trips and revocation preserves unrelated extensions', () => {
  const detail = ui.parseModelResource({ model: {
    model_id: 'mdl_local', model_client_config: { model_name: 'local', api_base: model.api_base,
      client_provider: 'OpenAI', extensions: model.extensions },
  } });
  const draft = ui.createModelDraft(detail, catalog);
  assert.equal(draft.kv_cache_affinity, true);
  const updated = ui.modelDraftToEntry({ ...draft, kv_cache_affinity: false }, detail, catalog, false);
  const payload = ui.buildModelResourcePayload(updated);
  assert.equal(payload.model_client_config.extensions.kv_cache.mode, 'none');
  assert.deepEqual(payload.model_client_config.extensions.thinking, { mode: 'auto' });
  assert.equal(payload.model_client_config.client_provider, 'OpenAI');
});

test('protocol and service changes revoke affinity; unrelated edits retain it', () => {
  const draft = ui.createModelDraft(model, catalog);
  assert.equal(ui.applyModelProtocol(draft, 'anthropic', catalog).kv_cache_affinity, false);
  assert.equal(ui.applyVendorSelection(draft, ui.CUSTOM_VENDOR_SELECTION, catalog).kv_cache_affinity, false);
  const renamed = ui.modelDraftToEntry({ ...draft, alias: 'Renamed' }, model, catalog, false);
  assert.equal(renamed.extensions.kv_cache.mode, 'affinity');
  const legacy = { ...model, model_provider: 'AscendAffinity' };
  const revoked = ui.modelDraftToEntry({ ...ui.createModelDraft(legacy, catalog), kv_cache_affinity: false }, legacy, catalog, false);
  assert.equal(revoked.model_provider, 'OpenAI');
});

test('alias edits preserve hidden legacy preset affinity and its provider', () => {
  const legacyCatalog = { ...catalog, custom_api: [{
    vendor_key: 'legacy', display_name: 'Legacy', plan: 'custom_api',
    client_provider: 'OpenAI', api_base: model.api_base, endpoint_profile: 'openai_compatible',
    default_model: model.model_name, model_options: [model.model_name], supports_anthropic: false,
  }] };
  for (const provider of ['OpenAI', 'AscendAffinity']) {
    const existing = { ...model, model_provider: provider, vendor_key: 'legacy', plan: 'custom_api' };
    const draft = ui.createModelDraft(existing, legacyCatalog);
    assert.notEqual(draft.vendor_selection, ui.CUSTOM_VENDOR_SELECTION);
    const next = ui.modelDraftToEntry({ ...draft, alias: 'Renamed' }, existing, legacyCatalog, false);
    const payload = ui.buildModelResourcePayload(next);
    assert.equal(payload.alias, 'Renamed');
    assert.equal(payload.model_client_config.client_provider, provider);
    assert.deepEqual(payload.model_client_config.extensions, existing.extensions);
    const changed = ui.modelDraftToEntry({ ...draft, model_name: 'other' }, existing, legacyCatalog, false);
    assert.equal(changed.extensions.kv_cache.mode, 'none');
    assert.equal(changed.model_provider, 'OpenAI');
  }
});

test('switching an affinity endpoint to Anthropic revokes the old declaration', () => {
  const draft = ui.applyModelProtocol(ui.createModelDraft(model, catalog), 'anthropic', catalog);
  const payload = ui.buildModelResourcePayload(ui.modelDraftToEntry(draft, model, catalog, true));
  assert.equal(payload.model_client_config.client_provider, 'Anthropic');
  assert.equal(payload.model_client_config.extensions.kv_cache.mode, 'none');
});

test('real model dialog can revoke the declaration and save the explicit mode', async (t) => {
  const saved = [];
  const view = await mount(t, createElement(ui.ModelDialog, {
    model, models: [model], catalog, catalogLoading: false, catalogError: '', saving: false,
    onClose() {}, onRetryCatalog() {}, onSave: async (next) => saved.push(next),
  }));
  const toggle = view.document.querySelector('[role="switch"][aria-label="This service supports Ascend KV-Cache affinity"]');
  assert.ok(toggle);
  assert.equal(toggle.getAttribute('aria-checked'), 'true');
  await act(async () => toggle.click());
  const save = view.document.querySelector('[data-testid="settings-model-dialog-save-btn"]');
  assert.ok(save);
  await act(async () => save.click());
  assert.equal(saved.length, 1);
  assert.equal(saved[0].extensions.kv_cache.mode, 'none');
});

test('Anthropic model dialogs do not offer the capability toggle', async (t) => {
  const view = await mount(t, createElement(ui.ModelDialog, {
    model: { ...model, model_provider: 'Anthropic', extensions: undefined }, models: [], catalog,
    catalogLoading: false, catalogError: '', saving: false, onClose() {}, onRetryCatalog() {}, onSave: async () => {},
  }));
  assert.equal(view.document.querySelector('[role="switch"][aria-label="This service supports Ascend KV-Cache affinity"]'), null);
});

const affinityItem = ui.experimentalModule.sections.find((section) => section.id === 'kv-cache-affinity').items[0];
const statusFor = (can_enable) => ({ requested_enabled: false, enabled: false, can_enable, scope: 'configured',
  models: [{ name: 'cloud', capable: can_enable }] });

const affinityPage = () => createElement(ui.SettingsPageLayout, { definition: {
  id: 'affinity-test', compositionMode: 'base',
  accessPolicy: { evaluate: () => ({ level: 'editable' }) },
  modules: [{ ...ui.experimentalModule, sections: [ui.experimentalModule.sections.find((section) => section.id === 'kv-cache-affinity')] }],
} });

test('no supporting model keeps the switch off and shows a localized top toast', async (t) => {
  const calls = [];
  const view = await mount(t, affinityPage(), {}, async (method, payload) => {
    calls.push({ method, payload });
    if (method === 'config.get') return { kv_cache_affinity_status: statusFor(false) };
    throw new Error('Cannot enable KV cache affinity: No configured model declares KV cache affinity support');
  });
  const toggle = view.document.querySelector('[role="switch"]');
  await act(async () => toggle.click());
  assert.equal(toggle.getAttribute('aria-checked'), 'false');
  assert.deepEqual(calls.map(({ method }) => method), ['config.get', 'config.save_all']);
  const header = view.document.querySelector('[data-testid="settings-header"]');
  assert.equal(header.querySelector('[role="alert"]'), null);
  assert.match(view.document.querySelector('[data-testid="settings-item-error-toast"]').textContent, /No configured model supports affinity/);
  assert.equal(view.document.querySelector('dialog[open]'), null);
  assert.doesNotMatch(view.document.querySelector('[data-testid="settings-module"]').textContent, /configuration check/i);
});

test('activation saves directly and becomes blue only after backend success', async (t) => {
  const calls = [];
  let finish;
  const view = await mount(t, affinityPage(), {}, async (method, payload) => {
    calls.push({ method, payload });
    if (method === 'config.get') return { kv_cache_affinity_status: statusFor(true) };
    return new Promise((resolve) => { finish = resolve; });
  });
  const toggle = view.document.querySelector('[role="switch"]');
  await act(async () => toggle.click());
  assert.equal(toggle.getAttribute('aria-checked'), 'false');
  assert.equal(toggle.disabled, true);
  assert.deepEqual(calls.map(({ method }) => method), ['config.get', 'config.save_all']);
  assert.equal(calls[1].payload.config.kv_cache_affinity_enabled, 'true');
  await act(async () => finish({ kv_cache_affinity_status: { ...statusFor(true), enabled: true, requested_enabled: true } }));
  assert.equal(toggle.getAttribute('aria-checked'), 'true');
  assert.equal(toggle.disabled, false);
});

test('connection errors keep the switch off and retain their own error message', async (t) => {
  const view = await mount(t, affinityPage(), {}, async (method) => {
    if (method === 'config.get') return { kv_cache_affinity_status: statusFor(true) };
    throw new Error('Connection lost');
  });
  await act(async () => view.document.querySelector('[role="switch"]').click());
  assert.equal(view.document.querySelector('[role="switch"]').getAttribute('aria-checked'), 'false');
  assert.match(view.document.querySelector('[data-testid="settings-item-error-toast"]').textContent, /Connection lost/);
  assert.doesNotMatch(view.document.querySelector('[data-testid="settings-item-error-toast"]').textContent, /No configured model/);
});

test('disabling saves directly and keeps capability declarations intact', async (t) => {
  const calls = [];
  const view = await mount(t, affinityPage(), {}, async (method, payload) => {
    calls.push({ method, payload });
    if (method === 'config.get') return { kv_cache_affinity_status: { ...statusFor(true), enabled: true, requested_enabled: true } };
    return { kv_cache_affinity_status: statusFor(true) };
  });
  await act(async () => view.document.querySelector('[role="switch"]').click());
  assert.deepEqual(calls.map(({ method }) => method), ['config.get', 'config.save_all']);
  assert.deepEqual(calls[1].payload.config, { kv_cache_affinity_enabled: 'false' });
  assert.equal(view.document.querySelector('[role="switch"]').getAttribute('aria-checked'), 'false');
});

test('config readback without valid capability status never presents a blue switch', async (t) => {
  function Reader() {
    const { config } = ui.useSettingsConfig();
    return createElement('div', { 'data-testid': 'readback' }, String(config.kv_cache_affinity_enabled));
  }
  const view = await mount(t, createElement(Reader), {}, async () => ({ kv_cache_affinity_enabled: 'true' }));
  assert.equal(view.document.querySelector('[data-testid="readback"]').textContent, 'false');
});
