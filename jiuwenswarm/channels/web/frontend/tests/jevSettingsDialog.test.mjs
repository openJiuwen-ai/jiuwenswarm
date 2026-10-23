import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><body><div id="root"></div></body>', {
  url: 'http://localhost:5176', pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'navigator', 'Node', 'HTMLElement', 'HTMLDialogElement']) {
  Object.defineProperty(globalThis, name, { configurable: true, value: name === 'window' ? dom.window : dom.window[name] });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');

await build({
  stdin: {
    contents: `
      import { createElement } from 'react';
      import { SettingsSourceProvider } from './src/features/settings/services/SettingsSourceProvider';
      import { JevSettings } from './src/features/settings/modules/models/JevSettings';
      import { browserModule } from './src/features/settings/modules/browser/definition';
      import { experimentalModule } from './src/features/settings/modules/experimental/definition';
      export { SettingsSaveQueue } from './src/features/settings/services/SettingsSaveQueue';
      export { SettingsUnsavedChangesRegistry } from './src/features/settings/services/SettingsUnsavedChangesRegistry';
      export { browserModule, experimentalModule };
      export const Fixture = () => createElement(SettingsSourceProvider, { source: 'config' }, createElement(JevSettings, {disabled: false}));
    `,
    resolveDir: process.cwd(), loader: 'ts',
  },
  outfile: 'node_modules/.cache/jev-settings-dialog.mjs',
  bundle: true, platform: 'node', format: 'esm', packages: 'external',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [{
    name: 'settings-boundaries',
    setup(builder) {
      const ui = ['Button', 'Input', 'Switch', 'FieldError', 'Loading', 'Select', 'Dialog', 'HelpTips', 'RadioGroup', 'Textarea']
        .map((name) => `export { ${name} } from './src/components/ui/${name}/${name}';`).join('\n');
      builder.onResolve({ filter: /(?:^|\/)ui$|SettingsServicesProvider$|^\.\.\/\.\.\/components$|^react-i18next$|^\.\/ExperimentalSettings$|^\.\/A4PSettings$/ }, (args) => ({
        path: args.path, namespace: 'boundary',
      }));
      builder.onLoad({ filter: /.*/, namespace: 'boundary' }, ({ path }) => ({
        contents: path === 'react-i18next'
          ? 'const t = key => key; export const useTranslation = () => ({ t });'
          : path === './ExperimentalSettings'
            ? ['A2UISetting', 'ExternalCliCustomAgentsItem', 'ProactiveLimitsSetting', 'RSISetting', 'TrajectoryDiagnosisSetting', 'TrajectoryUiSetting']
              .map(name => `export const ${name} = () => null;`).join('\n')
          : path === './A4PSettings' ? 'export const A4PSettings = () => null;'
          : path.endsWith('SettingsServicesProvider')
            ? 'export const useSettingsServices = () => globalThis.jevFixture;'
            : path === '../../components'
              ? `export { SettingRow } from './src/features/settings/components/SettingRow';
                 export { SettingsSection } from './src/features/settings/components/SettingsSection';
                 export { SettingsConfirmDialog } from './src/features/settings/components/SettingsConfirmDialog';`
              : ui,
        loader: 'js', resolveDir: process.cwd(),
      }));
    },
  }],
});
const { Fixture, SettingsSaveQueue, SettingsUnsavedChangesRegistry, browserModule, experimentalModule } = await import('../node_modules/.cache/jev-settings-dialog.mjs');
const get = (id, variant) => document.querySelector(`[data-testid="${id}"]${variant ? `[data-variant="${variant}"]` : ''}`);
const field = (name) => get('settings-browser-jev-dialog-field-input', name);
async function click(id, variant) {
  const element = get(id, variant);
  assert.ok(element, `Missing ${id}:${variant ?? ''}`);
  await act(async () => element.click());
}
async function input(name, value) {
  const element = field(name);
  assert.ok(element, `Missing ${name}`);
  await act(async () => {
    Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set.call(element, value);
    element.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
  });
}
async function choose(provider) {
  await act(async () => field('provider').click());
  await click('ui-select-option', provider);
}
async function mount(run, rejectSave = false) {
  const calls = [];
  const config = {
    jev_openrouter_api_key: 'router-key', jev_typesafe_api_key: 'typesafe-key',
    moa_jev_enabled: 'true',
  };
  let browser = {
    chrome_path: '', headless: false, decision_mode: 'hybrid', decision_provider: 'openrouter',
    decision_model: 'typesafe/jev-1.13', decision_api_base: 'https://openrouter.ai/api/alpha',
  };
  const services = {
    isConnected: true, saveQueue: new SettingsSaveQueue(), unsavedChanges: new SettingsUnsavedChangesRegistry(),
    request: async (method, params) => {
      calls.push({ method, params });
      if (method === 'config.get') return { ...config };
      if (method === 'config.save_all') {
        if (rejectSave) throw new Error('synthetic save failure');
        Object.assign(config, params.config);
        return {};
      }
      if (method === 'path.set') browser = { ...browser, ...params };
      if (method === 'path.get' || method === 'path.set') return { ...browser };
      throw new Error(`Unexpected ${method}`);
    },
  };
  globalThis.jevFixture = services;
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(Fixture)));
    await click('settings-browser-jev-configure-btn');
    await run({ calls, config, browser: () => browser });
  } finally {
    await act(async () => root.unmount());
    services.saveQueue.clear();
  }
}

test('provider templates preserve drafts and saved keys; custom saves then reopens', async () => {
  await mount(async ({ calls, browser }) => {
    assert.equal(field('api_key').value, 'router-key');
    assert.equal(field('api_key').type, 'password');
    assert.equal(field('api_base'), null);
    assert.equal(field('protocol'), null);
    await input('model', 'typesafe/jev-1.14');
    await choose('typesafe');
    assert.equal(field('api_key').value, 'typesafe-key');
    assert.equal(field('model').value, 'jev-1.13.0');
    await choose('openrouter');
    assert.equal(field('model').value, 'typesafe/jev-1.14');
    await choose('custom');
    assert.equal(field('api_key').value, '');
    assert.equal(field('api_base').value, '');
    await click('settings-browser-jev-dialog-save-btn');
    assert.equal(calls.filter(({ method }) => method.endsWith('.set') || method === 'config.save_all').length, 0);
    await input('api_base', 'https://jev.example.test/v1');
    await input('api_key', 'custom-key');
    await input('model', 'jev-preview');
    await click('settings-browser-jev-dialog-save-btn');
    assert.equal(get('settings-browser-jev-dialog'), null);
    assert.deepEqual(calls.filter(({ method }) => method === 'config.save_all' || method === 'path.set').map(({ method }) => method), ['config.save_all', 'path.set']);
    assert.equal(browser().decision_provider, 'custom');
    assert.equal(browser().decision_mode, 'hybrid');
    await click('settings-browser-jev-configure-btn');
    assert.equal(field('api_base').value, 'https://jev.example.test/v1');
    assert.equal(field('model').value, 'jev-preview');
    assert.equal(field('api_key').value, 'custom-key');
  });
});

test('a failed credential save leaves the provider unchanged and the dialog open', async () => {
  await mount(async ({ calls, browser }) => {
    await choose('typesafe');
    await input('api_key', 'new-typesafe-key');
    await click('settings-browser-jev-dialog-save-btn');
    assert.equal(browser().decision_provider, 'openrouter');
    assert.ok(get('settings-browser-jev-dialog-error').textContent.includes('synthetic save failure'));
    assert.equal(calls.filter(({ method }) => method === 'path.set').length, 0);
  }, true);
});

async function confirmDialog(label) {
  const dialog = [...document.querySelectorAll('dialog[open]')].at(-1);
  const button = [...dialog.querySelectorAll('button')].find((element) => element.textContent === label);
  assert.ok(button, `Missing confirmation ${label}`);
  await act(async () => button.click());
}

test('cancel discards unsaved provider changes without issuing a write', async () => {
  await mount(async ({ calls }) => {
    await choose('custom');
    await input('api_base', 'https://unsaved.example.test/v1');
    await click('settings-browser-jev-dialog-cancel-btn');
    await confirmDialog('common.confirm');
    assert.equal(get('settings-browser-jev-dialog'), null);
    assert.equal(calls.filter(({ method }) => method === 'path.set' || method === 'config.save_all').length, 0);
    await click('settings-browser-jev-configure-btn');
    assert.equal(field('api_key').value, 'router-key');
    assert.equal(field('api_base'), null);
  });
});

test('clearing the active credential disables Jev first and preserves other providers', async () => {
  await mount(async ({ calls, config, browser }) => {
    await click('settings-browser-jev-dialog-cancel-btn');
    await click('settings-browser-jev-clear-btn');
    await confirmDialog('settingsPanel.common.clear');
    assert.equal(browser().decision_mode, 'llm');
    assert.equal(config.jev_openrouter_api_key, '');
    assert.equal(config.jev_typesafe_api_key, 'typesafe-key');
    assert.equal(config.moa_jev_enabled, 'false');
    assert.deepEqual(calls.filter(({ method }) => method === 'path.set' || method === 'config.save_all').map(({ method }) => method), ['path.set', 'config.save_all']);
  });
});

test('one shared Jev configuration is shown; mode and switches stay in their original modules', async () => {
  const browserItems = browserModule.sections.flatMap(section => section.items);
  assert.deepEqual(browserItems.map(item => item.id), ['browser-path', 'browser-run-mode', 'browser-decision-mode']);
  const collaboration = experimentalModule.sections.find(section => section.id === 'multi-model-collaboration');
  assert.deepEqual(collaboration.items[0].subItems.items[0], {
    id: 'collaboration-jev-enabled', component: 'switch', key: 'moa_jev_enabled',
  });
  assert.equal(collaboration.items[0].subItems.items[1].render.name, 'CollaborationVerifierInterval');
  await mount(async ({ calls, config, browser }) => {
    await click('settings-browser-jev-dialog-cancel-btn');
    assert.ok(get('settings-models-jev-panel'));
    assert.equal(get('settings-select', 'browser-decision-mode'), null);
    assert.equal(document.querySelector('[role="switch"]'), null);
    assert.equal(get('settings-collaboration-jev-configure-btn'), null);
    assert.equal(document.querySelectorAll('[data-testid="settings-browser-jev-configure-btn"]').length, 1);
    await click('settings-browser-jev-configure-btn');
    await input('api_key', 'updated-shared-key');
    await click('settings-browser-jev-dialog-save-btn');
    assert.equal(config.moa_jev_api_key, undefined);
    assert.equal(config.jev_openrouter_api_key, 'updated-shared-key');
    assert.equal(config.jev_typesafe_api_key, 'typesafe-key');
    assert.equal(config.multi_model_collaboration_enabled, undefined);
    assert.equal(config.moa_jev_enabled, 'true');
    assert.equal(browser().decision_provider, 'openrouter');
    assert.equal(calls.filter(({ method }) => method === 'path.set').length, 1);
    await click('settings-browser-jev-configure-btn');
    assert.equal(field('api_key').value, 'updated-shared-key');
  });
});

test.after(() => dom.window.close());
