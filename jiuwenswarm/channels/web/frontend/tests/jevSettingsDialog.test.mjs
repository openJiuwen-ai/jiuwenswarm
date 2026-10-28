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
      import { JEV_PRESETS } from './src/features/settings/modules/browser/jevSettings';
      import { SettingsSourceProvider } from './src/features/settings/services/SettingsSourceProvider';
      import { JevSettings } from './src/features/settings/modules/models/JevSettings';
      import { browserModule } from './src/features/settings/modules/browser/definition';
      import { experimentalModule } from './src/features/settings/modules/experimental/definition';
      export { SettingsSaveQueue } from './src/features/settings/services/SettingsSaveQueue';
      export { SettingsUnsavedChangesRegistry } from './src/features/settings/services/SettingsUnsavedChangesRegistry';
      export { browserModule, experimentalModule, JEV_PRESETS };
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
      builder.onResolve({ filter: /(?:^|\/)ui$|SettingsServicesProvider$|^\.\.\/\.\.\/components$|^react-i18next$|^\.\/ExperimentalSettings$|^\.\/A4PSettings$|assets\/(?:settings|providers)$/ }, (args) => ({
        path: args.path, namespace: 'boundary',
      }));
      builder.onLoad({ filter: /.*/, namespace: 'boundary' }, ({ path }) => ({
        contents: path === 'react-i18next'
          ? 'const t = key => key; export const useTranslation = () => ({ t });'
          : path.endsWith('assets/settings')
            ? `import { createElement } from 'react';
               export const settingsActionIcons = { edit: () => createElement('svg'), delete: () => createElement('svg') };
               export const settingsNavigationIcons = { browser: () => null, experimental: () => null };
               export const settingsEmptyBoxIllustration = 'empty.svg';`
          : path.endsWith('assets/providers')
            ? `export const getModelLogoUrl = ({ vendor_key }) => 'provider-' + vendor_key + '.svg';`
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
const { Fixture, SettingsSaveQueue, SettingsUnsavedChangesRegistry, browserModule, experimentalModule, JEV_PRESETS } = await import('../node_modules/.cache/jev-settings-dialog.mjs');
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
async function mount(run, rejectSave = false, options = {}) {
  const calls = [];
  const config = {
    jev_openrouter_api_key: 'router-key', jev_typesafe_api_key: 'typesafe-key',
    moa_jev_enabled: 'true',
    ...options.config,
  };
  let browser = {
    chrome_path: '', headless: false, decision_mode: 'hybrid', decision_provider: 'openrouter',
    decision_model: 'typesafe/jev-1.13', decision_api_base: 'https://openrouter.ai/api/alpha',
    decision_display_name: 'Shared Jev',
    ...options.browser,
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
      if (method === 'path.set') {
        if (options.rejectPathSet) throw new Error('synthetic browser save failure');
        browser = {
          ...browser,
          ...Object.fromEntries(Object.entries(params).filter(([, value]) => value !== undefined)),
        };
      }
      if (method === 'path.get' || method === 'path.set') return {
        ...browser,
        jev_key_configured: Boolean(browser.decision_provider && config[JEV_PRESETS[browser.decision_provider].keyField]),
      };
      throw new Error(`Unexpected ${method}`);
    },
  };
  globalThis.jevFixture = services;
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(Fixture)));
    if (options.openDialog !== false) await click('settings-models-jev-edit-btn');
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
    assert.equal(field('context_window_tokens'), null);
    assert.equal(field('display_name').value, 'Shared Jev');
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
    await input('display_name', 'Custom decision model');
    await click('settings-browser-jev-dialog-save-btn');
    assert.equal(get('settings-browser-jev-dialog'), null);
    assert.deepEqual(calls.filter(({ method }) => method === 'config.save_all' || method === 'path.set').map(({ method }) => method), ['config.save_all', 'path.set']);
    assert.equal(browser().decision_provider, 'custom');
    assert.equal(browser().decision_mode, 'hybrid');
    assert.equal(browser().decision_display_name, 'Custom decision model');
    assert.equal(get('settings-models-jev-card-title').textContent, 'Custom decision model');
    assert.equal(get('settings-models-jev-card-metadata').textContent, 'settingsPanel.browser.providerCustom · jev-preview');
    assert.equal(get('settings-models-jev-add-btn'), null);
    await click('settings-models-jev-edit-btn');
    assert.equal(field('api_base').value, 'https://jev.example.test/v1');
    assert.equal(field('model').value, 'jev-preview');
    assert.equal(field('api_key').value, 'custom-key');
    assert.equal(field('display_name').value, 'Custom decision model');
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
    await click('settings-models-jev-edit-btn');
    assert.equal(field('api_key').value, 'router-key');
    assert.equal(field('api_base'), null);
  });
});

for (const provider of ['openrouter', 'typesafe', 'custom']) {
  test(`removing ${provider} Jev uses existing saves and retains its connection`, async () => {
    const preset = JEV_PRESETS[provider];
    const endpoint = provider === 'custom' ? 'https://private.example.test/v1' : preset.api_base;
    const credentials = {
      jev_openrouter_api_key: 'router-key', jev_typesafe_api_key: 'typesafe-key',
      jev_custom_api_key: 'custom-key', api_key: 'primary-key',
    };
    await mount(async ({ calls, config, browser }) => {
      await click('settings-browser-jev-dialog-cancel-btn');
      await click('settings-models-jev-delete-btn');
      await confirmDialog('common.delete');
      assert.equal(browser().decision_mode, 'llm');
      for (const [key, value] of Object.entries(credentials)) {
        assert.equal(config[key], key === preset.keyField ? '' : value);
      }
      assert.equal(config.moa_jev_enabled, 'false');
      assert.equal(browser().decision_display_name, '');
      assert.ok(get('settings-models-jev-empty'));
      assert.equal(get('settings-models-jev-card'), null);
      assert.equal(browser().decision_provider, provider);
      assert.equal(browser().decision_model, preset.model);
      assert.equal(browser().decision_api_base, endpoint);
      assert.equal(browser().headless, false);
      const writes = calls.filter(({ method }) => ['path.set', 'config.save_all'].includes(method));
      assert.deepEqual(writes.map(({ method }) => method), ['path.set', 'config.save_all']);
      assert.equal(writes[0].params.decision_mode, 'llm');
      assert.equal(writes[0].params.decision_display_name, '');
      assert.equal(writes[0].params.decision_model, undefined);
      assert.equal(writes[0].params.decision_api_base, undefined);
      assert.deepEqual(writes[1].params.config, { [preset.keyField]: '', moa_jev_enabled: 'false' });
      await click('settings-models-jev-empty-add-btn');
      assert.equal(field('provider').textContent, 'settingsPanel.models.vendorPlaceholder');
      assert.equal(field('model').value, '');
      assert.equal(field('display_name').value, '');
      await choose(provider);
      assert.equal(field('api_key').value, '');
      assert.equal(field('display_name').value, preset.model);
      if (provider === 'custom') assert.equal(field('api_base').value, '');
      const nextModel = provider === 'openrouter' ? 'typesafe/jev-1.14' : 'jev-next';
      await input('model', nextModel);
      await act(async () => field('model').dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true })));
      assert.equal(field('display_name').value, nextModel);
      await input('api_key', 'new-provider-key');
      if (provider === 'custom') await input('api_base', endpoint);
      await click('settings-browser-jev-dialog-save-btn');
      assert.equal(get('settings-browser-jev-dialog'), null);
      assert.equal(config[preset.keyField], 'new-provider-key');
      assert.equal(browser().decision_mode, 'llm');
      assert.equal(config.moa_jev_enabled, 'false');
      assert.equal(get('settings-models-jev-card-title').textContent, nextModel);
    }, false, {
      config: credentials,
      browser: {
        decision_mode: provider === 'typesafe' ? 'llm' : 'hybrid', decision_provider: provider,
        decision_model: preset.model, decision_api_base: endpoint,
      },
    });
  });
}

test('a failed browser clear does not clear credentials or collaboration settings', async () => {
  await mount(async ({ calls, config, browser }) => {
    await click('settings-browser-jev-dialog-cancel-btn');
    await click('settings-models-jev-delete-btn');
    await confirmDialog('common.delete');
    assert.ok(get('settings-models-jev-card'));
    assert.equal(browser().decision_provider, 'openrouter');
    assert.equal(browser().decision_display_name, 'Shared Jev');
    assert.equal(config.jev_openrouter_api_key, 'router-key');
    assert.equal(config.moa_jev_enabled, 'true');
    assert.match(document.body.textContent, /settingsPanel.models.jevDeleteFailed/);
    assert.deepEqual(calls.filter(({ method }) => ['path.set', 'config.save_all'].includes(method)).map(({ method }) => method), ['path.set']);
  }, false, { rejectPathSet: true });
});

test('a failed credential clear reports failure and retains the already saved browser changes', async () => {
  await mount(async ({ calls, config, browser }) => {
    await click('settings-browser-jev-dialog-cancel-btn');
    await click('settings-models-jev-delete-btn');
    await confirmDialog('common.delete');
    assert.equal(browser().decision_mode, 'llm');
    assert.equal(browser().decision_display_name, '');
    assert.equal(browser().decision_provider, 'openrouter');
    assert.equal(config.jev_openrouter_api_key, 'router-key');
    assert.equal(config.moa_jev_enabled, 'true');
    assert.ok(get('settings-models-jev-card'));
    assert.match(document.body.textContent, /settingsPanel.models.jevDeleteFailed/);
    assert.deepEqual(calls.filter(({ method }) => ['path.set', 'config.save_all'].includes(method)).map(({ method }) => method), ['path.set', 'config.save_all']);
  }, true);
});

test('one shared Jev configuration is shown; mode and switches stay in their original modules', async () => {
  const browserItems = browserModule.sections.flatMap(section => section.items);
  assert.deepEqual(browserItems.map(item => item.id), ['browser-path', 'browser-run-mode', 'browser-decision-mode']);
  const collaboration = experimentalModule.sections.find(section => section.id === 'multi-model-collaboration');
  assert.deepEqual(collaboration.items[0].subItems.items[0], {
    id: 'collaboration-jev-enabled', component: 'switch', key: 'moa_jev_enabled', errorDisplay: 'inline',
  });
  assert.equal(collaboration.items[0].subItems.items[1].render.name, 'CollaborationVerifierInterval');
  await mount(async ({ calls, config, browser }) => {
    await click('settings-browser-jev-dialog-cancel-btn');
    assert.ok(get('settings-models-jev-panel'));
    assert.equal(get('settings-select', 'browser-decision-mode'), null);
    assert.equal(document.querySelector('[role="switch"]'), null);
    assert.equal(get('settings-collaboration-jev-configure-btn'), null);
    assert.equal(document.querySelectorAll('[data-testid="settings-models-jev-card"]').length, 1);
    assert.equal(get('settings-models-jev-add-btn'), null);
    await click('settings-models-jev-edit-btn');
    await input('api_key', 'updated-shared-key');
    await click('settings-browser-jev-dialog-save-btn');
    assert.equal(config.moa_jev_api_key, undefined);
    assert.equal(config.jev_openrouter_api_key, 'updated-shared-key');
    assert.equal(config.jev_typesafe_api_key, 'typesafe-key');
    assert.equal(config.multi_model_collaboration_enabled, undefined);
    assert.equal(config.moa_jev_enabled, 'true');
    assert.equal(browser().decision_provider, 'openrouter');
    assert.equal(calls.filter(({ method }) => method === 'path.set').length, 1);
    await click('settings-models-jev-edit-btn');
    assert.equal(field('api_key').value, 'updated-shared-key');
  });
});

test('an empty Jev configuration can add exactly one named model and then edit it', async () => {
  await mount(async ({ browser, calls }) => {
    assert.ok(get('settings-models-jev-empty'));
    assert.equal(get('settings-models-jev-add-btn'), null);
    assert.equal(get('settings-models-jev-card'), null);
    await click('settings-models-jev-empty-add-btn');
    assert.equal(get('settings-browser-jev-dialog-title').textContent, 'settingsPanel.models.addModel');
    assert.equal(field('provider').textContent, 'settingsPanel.models.vendorPlaceholder');
    assert.equal(field('api_key').disabled, true);
    assert.equal(field('model').disabled, true);
    assert.equal(field('display_name').disabled, true);
    await click('settings-browser-jev-dialog-save-btn');
    assert.equal(calls.filter(({ method }) => method === 'path.set' || method === 'config.save_all').length, 0);
    await act(async () => field('provider').click());
    for (const provider of ['openrouter', 'typesafe', 'custom']) {
      const option = get('ui-select-option', provider);
      assert.ok(option.querySelector('img').src.endsWith(`provider-${provider}.svg`));
    }
    await click('ui-select-option', 'openrouter');
    assert.ok(field('provider').querySelector('img').src.endsWith('provider-openrouter.svg'));
    await input('api_key', 'new-key');
    await input('display_name', 'My decision model');
    await click('settings-browser-jev-dialog-save-btn');
    assert.equal(browser().decision_display_name, 'My decision model');
    assert.equal(get('settings-models-jev-empty'), null);
    assert.equal(get('settings-models-jev-add-btn'), null);
    assert.equal(document.querySelectorAll('[data-testid="settings-models-jev-card"]').length, 1);
    assert.equal(get('settings-models-jev-card-title').textContent, 'My decision model');
    assert.equal(get('settings-models-jev-card-metadata').textContent, 'OpenRouter · typesafe/jev-1.13');
    assert.ok(get('settings-models-jev-card').querySelector('img').src.endsWith('provider-openrouter.svg'));
    await click('settings-models-jev-edit-btn');
    assert.equal(get('settings-browser-jev-dialog-title').textContent, 'settingsPanel.models.editModel');
    assert.equal(field('display_name').value, 'My decision model');
  }, false, {
    config: { jev_openrouter_api_key: '', jev_typesafe_api_key: '' },
    browser: { decision_mode: 'llm', decision_display_name: undefined },
    openDialog: false,
  });
});

test('invalid display names prevent writes and customized names survive provider changes', async () => {
  await mount(async ({ calls }) => {
    for (const name of [' ', 'x'.repeat(101)]) {
      await input('display_name', name);
      await click('settings-browser-jev-dialog-save-btn');
      assert.ok(get('settings-browser-jev-dialog'));
      assert.equal(calls.filter(({ method }) => method === 'path.set' || method === 'config.save_all').length, 0);
    }
    await input('display_name', 'A custom name');
    await choose('typesafe');
    assert.equal(field('display_name').value, 'A custom name');
    await input('model', 'jev-next');
    await act(async () => field('model').dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true })));
    assert.equal(field('display_name').value, 'A custom name');
  });
});

test('legacy names start from the model and follow model edits until customized', async () => {
  await mount(async () => {
    assert.equal(field('display_name').value, 'typesafe/jev-1.13');
    await input('model', 'typesafe/jev-1.14');
    await act(async () => field('model').dispatchEvent(new dom.window.FocusEvent('focusout', { bubbles: true })));
    assert.equal(field('display_name').value, 'typesafe/jev-1.14');
  }, false, { browser: { decision_display_name: undefined } });
});

test('adding ignores the provider and connection left behind by earlier deletions', async () => {
  await mount(async () => {
    await click('settings-models-jev-empty-add-btn');
    assert.equal(field('provider').textContent, 'settingsPanel.models.vendorPlaceholder');
    assert.equal(field('model').value, '');
    assert.equal(field('display_name').value, '');
    assert.equal(field('api_base'), null);
    await choose('openrouter');
    assert.equal(field('model').value, 'typesafe/jev-1.13');
    assert.equal(field('display_name').value, 'typesafe/jev-1.13');
  }, false, {
    config: { jev_custom_api_key: '' },
    browser: {
      decision_mode: 'llm', decision_provider: 'custom', decision_display_name: 'An obsolete name',
      decision_model: 'previous-model', decision_api_base: 'https://previous.example.test/v1',
    },
    openDialog: false,
  });
});

test.after(() => dom.window.close());
