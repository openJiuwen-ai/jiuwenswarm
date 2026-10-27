import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

// Initialize the browser before ReactDOM loads so controlled input events use its browser path.
const boot = new JSDOM('<!doctype html><html><body></body></html>', { url: 'http://localhost:5173' });
for (const [key, value] of Object.entries({
  window: boot.window,
  document: boot.window.document,
  Node: boot.window.Node,
  HTMLElement: boot.window.HTMLElement,
  navigator: boot.window.navigator,
  IS_REACT_ACT_ENVIRONMENT: true,
}))
  Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');

await build({
  stdin: {
    contents: [
      'import { createElement } from "react";',
      'import { SettingsSourceProvider } from "./src/features/settings/services/SettingsSourceProvider";',
      'import { SettingItemRenderer } from "./src/features/settings/components/SettingItemRenderer";',
      'import { experimentalModule } from "./src/features/settings/modules/experimental/definition";',
      'import { useSettingsConfig } from "./src/features/settings/services/useSettingsConfig";',
      'export const ConfigFixture = () => { const controller = useSettingsConfig(); globalThis.collaborationFixture.controller = controller; return createElement("div", null, controller.loading ? "loading" : "loaded"); };',
      'export { SettingsSaveQueue } from "./src/features/settings/services/SettingsSaveQueue";',
      'export { SettingsUnsavedChangesRegistry } from "./src/features/settings/services/SettingsUnsavedChangesRegistry";',
      'export * from "./src/features/settings/modules/experimental/multiModelCollaborationSettingsState";',
      'export * from "./src/features/multiModelCollaboration/featureConfig";',
      'export const experimentalSectionIds = experimentalModule.sections.map(section => section.id);',
      'export const SettingsFixture = () => createElement(SettingsSourceProvider, {source:"config"}, createElement(SettingItemRenderer, {item:experimentalModule.sections.find(section => section.id === "multi-model-collaboration").items[0], disabled:globalThis.collaborationFixture.disabled}));',
    ].join('\n'),
    resolveDir: process.cwd(),
    loader: 'ts',
  },
  outfile: 'node_modules/.cache/multi-model-collaboration-settings.mjs',
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [
    {
      name: 'collaboration-settings-boundaries',
      setup(builder) {
        const ui = [
          'Button',
          'Input',
          'Switch',
          'FieldError',
          'Loading',
          'Select',
          'Dialog',
          'HelpTips',
          'RadioGroup',
          'Textarea',
        ]
          .map((name) => `export { ${name} } from "./src/components/ui/${name}/${name}";`)
          .join('\n');
        const mocks = {
          '../../../../components/ui': ui,
          '../../../components/ui': ui,
          '../../ui': ui,
          '../ui': ui,
          '../../components': [
            'export { SettingRow } from "./src/features/settings/components/SettingRow";',
            'export { SettingsConfirmDialog } from "./src/features/settings/components/SettingsConfirmDialog";',
          ].join('\n'),
          '../../services/SettingsServicesProvider':
            'export const useSettingsServices = () => globalThis.collaborationFixture.services;',
          '../services/SettingsServicesProvider':
            'export const useSettingsServices = () => globalThis.collaborationFixture.services;',
          './SettingsServicesProvider':
            'export const useSettingsServices = () => globalThis.collaborationFixture.services;',
          './ExperimentalSettings': [
            'A2UISetting',
            'ExternalCliCustomAgentsItem',
            'ProactiveLimitsSetting',
            'RSISetting',
            'TaskAsrSetting',
            'TaskFullDuplexSetting',
            'TrajectoryDiagnosisSetting',
          ]
            .map((name) => `export const ${name} = () => null;`)
            .join('\n'),
          './A4PSettings': 'export const A4PSettings = () => null;',
          '../../../../assets/settings': 'export const settingsNavigationIcons = {experimental: () => null};',
          'react-i18next':
            'export const useTranslation = () => ({ t: (key) => key, i18n:{resolvedLanguage:"zh",language:"zh"} });',
        };
        builder.onResolve({ filter: /.*/ }, (args) =>
          mocks[args.path] ? { path: args.path, namespace: 'mock' } : undefined,
        );
        builder.onLoad({ filter: /.*/, namespace: 'mock' }, (args) => ({
          contents: mocks[args.path],
          loader: 'js',
          resolveDir: process.cwd(),
        }));
      },
    },
  ],
});
const {
  SettingsFixture,
  ConfigFixture,
  SettingsSaveQueue,
  SettingsUnsavedChangesRegistry,
  parseCollaborationVerifierInterval,
  isMultiModelCollaborationEnabled,
  setMultiModelCollaborationEnabled,
  getMultiModelCollaborationRevision,
  applyMultiModelCollaborationSnapshot,
  experimentalSectionIds,
} = await import('../node_modules/.cache/multi-model-collaboration-settings.mjs');

async function mount(options, run) {
  const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
    url: 'http://localhost:5173',
  });
  dom.window.HTMLDialogElement.prototype.showModal = function () {
    this.open = true;
  };
  dom.window.HTMLDialogElement.prototype.close = function () {
    this.open = false;
  };
  const globals = {
    window: dom.window,
    document: dom.window.document,
    Node: dom.window.Node,
    HTMLElement: dom.window.HTMLElement,
    navigator: dom.window.navigator,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const old = new Map(
    [...Object.keys(globals), 'collaborationFixture'].map((key) => [
      key,
      Object.getOwnPropertyDescriptor(globalThis, key),
    ]),
  );
  for (const [key, value] of Object.entries(globals))
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  const calls = [],
    saves = [];
  const saveQueue = new SettingsSaveQueue();
  const unsavedChanges = new SettingsUnsavedChangesRegistry();
  let config = { ...options.config };
  const services = {
    isConnected: options.connected !== false,
    saveQueue,
    unsavedChanges,
    request: async (method, params) => {
      calls.push({ method, params });
      if (method === 'config.get') return options.load ? options.load() : { ...config };
      if (method === 'config.save_all') {
        saves.push(params.config);
        if (options.rejectSave) throw new Error('save failed');
        if (options.save) await options.save(params.config);
        config = { ...config, ...params.config };
        return { saved: true };
      }
      throw new Error(`Unexpected RPC: ${method}`);
    },
  };
  globalThis.collaborationFixture = { services, disabled: options.disabled ?? false };
  setMultiModelCollaborationEnabled(false);
  const root = createRoot(document.getElementById('root'));
  const render = () => act(async () => root.render(createElement(options.probe ? ConfigFixture : SettingsFixture)));
  const get = (id, variant) =>
    document.querySelector(`[data-testid="${id}"]${variant ? `[data-variant="${variant}"]` : ''}`);
  const click = async (id, variant) => {
    const element = get(id, variant);
    assert.ok(element, `Missing ${id}:${variant ?? ''}`);
    await act(async () => element.click());
  };
  const input = async (id, value, variant) => {
    const element = get(id, variant);
    assert.ok(element, `Missing input ${id}:${variant ?? ''}`);
    const setter = Object.getOwnPropertyDescriptor(dom.window.HTMLInputElement.prototype, 'value').set;
    await act(async () => {
      setter.call(element, value);
      element.dispatchEvent(new dom.window.Event('input', { bubbles: true }));
    });
  };
  const key = async (id, value) =>
    act(async () => get(id).dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: value, bubbles: true })));
  const main = () => get('settings-switch', 'multi-model-collaboration-enabled');
  const modalButton = async (text) => {
    const modal = [...document.querySelectorAll('dialog[open]')].at(-1);
    const button = [...modal.querySelectorAll('button')].find((element) => element.textContent === text);
    assert.ok(button, `Missing modal action ${text}`);
    await act(async () => button.click());
  };
  try {
    await render();
    await run({
      get,
      click,
      input,
      key,
      main,
      modalButton,
      calls,
      saves,
      render,
      services,
      dom,
      unsavedChanges,
      config: () => config,
    });
  } finally {
    await act(async () => root.unmount());
    saveQueue.clear();
    setMultiModelCollaborationEnabled(false);
    dom.window.close();
    for (const [key, descriptor] of old) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
}

const enabled = { multi_model_collaboration_enabled: 'true' };
const configured = {
  ...enabled,
  jev_typesafe_api_key: 'shared-synthetic-key',
};

test('feature flag defaults off and accepts backend booleans', () => {
  setMultiModelCollaborationEnabled(undefined);
  assert.equal(isMultiModelCollaborationEnabled(), false);
  setMultiModelCollaborationEnabled('true');
  assert.equal(isMultiModelCollaborationEnabled(), true);
  setMultiModelCollaborationEnabled('false');
  assert.equal(isMultiModelCollaborationEnabled(), false);
});

test('collaboration and other experimental sections retain their order', () => {
  assert.deepEqual(experimentalSectionIds, [
    'external-cli-custom-agents',
    'rsi',
    'a2ui',
    'a4p',
    'trajectory-ui',
    'skill-pack',
    'kv-cache-affinity',
    'team-organization',
    'team-scheduled-dispatch',
    'proactive-recommendation',
    'multi-model-collaboration',
    'tool-optimize',
    'computer-control',
  ]);
});

test('registered main switch controls child visibility and preserves child config when disabled', async () => {
  await mount(
    { config: { ...configured, moa_jev_enabled: 'true', moa_verifier_interval: '7' } },
    async ({ main, get, click, saves }) => {
      assert.equal(main().getAttribute('aria-checked'), 'true');
      assert.equal(isMultiModelCollaborationEnabled(), true);
      assert.equal(get('settings-collaboration-interval-input').value, '7');
      await click('settings-switch', 'multi-model-collaboration-enabled');
      assert.deepEqual(saves, [{ multi_model_collaboration_enabled: 'false' }]);
      assert.equal(get('settings-collaboration-interval-input'), null);
      assert.equal(get('settings-switch', 'collaboration-jev-enabled'), null);
      assert.equal(isMultiModelCollaborationEnabled(), false);
      await click('settings-switch', 'multi-model-collaboration-enabled');
      assert.equal(get('settings-switch', 'collaboration-jev-enabled').getAttribute('aria-checked'), 'true');
      assert.equal(get('settings-collaboration-interval-input').value, '7');
    },
  );
});

test('empty config defaults to disabled main and expands default verifier values after enabling', async () => {
  await mount({}, async ({ get, main, click, saves }) => {
    assert.equal(main().getAttribute('aria-checked'), 'false');
    assert.equal(get('settings-collaboration-interval-input'), null);
    assert.equal(get('settings-collaboration-jev-configure-btn'), null);
    await click('settings-switch', 'multi-model-collaboration-enabled');
    assert.deepEqual(saves, [{ multi_model_collaboration_enabled: 'true' }]);
    assert.equal(get('settings-switch', 'collaboration-jev-enabled').getAttribute('aria-checked'), 'false');
    assert.equal(get('settings-collaboration-interval-input').value, '3');
  });
});

test('JEV switch rejects a missing key without opening or saving model configuration', async () => {
  await mount({ config: enabled, save: () => { throw new Error('JEV api_key is required'); } }, async ({ get, click, saves }) => {
    await click('settings-switch', 'collaboration-jev-enabled');
    assert.equal(get('settings-switch', 'collaboration-jev-enabled').getAttribute('aria-checked'), 'false');
    assert.equal(get('settings-collaboration-jev-dialog'), null);
    assert.deepEqual(saves, [{ moa_jev_enabled: 'true' }]);
  });
});

test('configured JEV switches directly without changing connection or browser settings', async () => {
  await mount({ config: configured }, async ({ click, get, saves }) => {
    await click('settings-switch', 'collaboration-jev-enabled');
    assert.equal(get('settings-collaboration-jev-dialog'), null);
    await click('settings-switch', 'collaboration-jev-enabled');
    assert.deepEqual(saves, [{ moa_jev_enabled: 'true' }, { moa_jev_enabled: 'false' }]);
  });
});

for (const raw of ['0', '-1', '1.5', '1e2', '', '9007199254740992']) {
  test(`invalid verification interval ${JSON.stringify(raw)} is never saved`, async () => {
    await mount({ config: enabled }, async ({ click, input, get, saves }) => {
      await click('settings-collaboration-interval-modify-btn');
      await input('settings-collaboration-interval-input', raw);
      assert.equal(get('settings-collaboration-interval-save-btn').disabled, true);
      assert.ok(get('settings-collaboration-interval-error'));
      assert.deepEqual(saves, []);
    });
  });
}

test('interval explicit save and Enter validate, Escape cancels, and no user turn count is persisted', async () => {
  await mount({ config: enabled }, async ({ get, click, input, key, saves, unsavedChanges }) => {
    await click('settings-collaboration-interval-modify-btn');
    await input('settings-collaboration-interval-input', '1');
    assert.equal(unsavedChanges.hasChanges(), true);
    await key('settings-collaboration-interval-input', 'Enter');
    assert.deepEqual(saves, [{ moa_verifier_interval: '1' }]);
    assert.equal(get('settings-collaboration-interval-input').readOnly, true);
    await click('settings-collaboration-interval-modify-btn');
    await input('settings-collaboration-interval-input', '9');
    await key('settings-collaboration-interval-input', 'Escape');
    assert.equal(get('settings-collaboration-interval-input').value, '1');
    assert.equal(unsavedChanges.hasChanges(), false);
    assert.equal(saves.length, 1);
  });
});

test('disconnect and read-only settings disable changes', async () => {
  await mount({ config: configured }, async ({ get, main, render, services, saves }) => {
    globalThis.collaborationFixture.disabled = true;
    await render();
    assert.equal(main().disabled, true);
    assert.equal(get('settings-switch', 'collaboration-jev-enabled').disabled, true);
    assert.equal(get('settings-collaboration-interval-modify-btn').disabled, true);
    globalThis.collaborationFixture.disabled = false;
    services.isConnected = false;
    await render();
    assert.equal(main().disabled, true);
    assert.equal(get('settings-switch', 'collaboration-jev-enabled').disabled, true);
    assert.deepEqual(saves, []);
  });
});

test('reconnect reloads confirmed feature flag and child config', async () => {
  let loadCount = 0;
  await mount(
    {
      load: () =>
        ++loadCount === 1 ? enabled : { multi_model_collaboration_enabled: false, moa_verifier_interval: 7 },
    },
    async ({ render, services, get, main }) => {
      assert.equal(isMultiModelCollaborationEnabled(), true);
      services.isConnected = false;
      await render();
      services.isConnected = true;
      await render();
      assert.equal(loadCount, 2);
      assert.equal(main().getAttribute('aria-checked'), 'false');
      assert.equal(isMultiModelCollaborationEnabled(), false);
      assert.equal(get('settings-collaboration-interval-input'), null);
      assert.equal(get('settings-collaboration-jev-configure-btn'), null);
    },
  );
});

test('independent MoA page and navigation entry are removed', () => {
  assert.equal(fs.existsSync('src/features/settings/modules/moa'), false);
  const definition = fs.readFileSync('src/features/settings/registry/openSourceDefinition.ts', 'utf8');
  assert.doesNotMatch(definition, /moaModule|modules\/moa/);
  const contract = fs.readFileSync('src/features/settings/services/settingsContract.ts', 'utf8');
  assert.doesNotMatch(contract, /moa_model_group_id|moa_aggregator_route_id|moa_verifier_route_id|moa_verifier_type/);
});

test('reconnect response cannot overwrite a completed feature save, but a new reload respects external changes', async () => {
  let resolveSave;
  let resolveReload;
  let loads = 0;
  await mount(
    {
      probe: true,
      load: () => {
        loads += 1;
        if (loads === 2)
          return new Promise((resolve) => {
            resolveReload = resolve;
          });
        return { multi_model_collaboration_enabled: false };
      },
      save: () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    },
    async ({ services, render }) => {
      let saving;
      await act(async () => {
        saving = globalThis.collaborationFixture.controller.save(
          { multi_model_collaboration_enabled: 'true' },
          'feature-flag',
        );
      });
      services.isConnected = false;
      await render();
      services.isConnected = true;
      await render();
      await act(async () => {
        resolveSave();
        await saving;
      });
      assert.equal(isMultiModelCollaborationEnabled(), true);
      await act(async () => resolveReload({ multi_model_collaboration_enabled: false }));
      assert.equal(globalThis.collaborationFixture.controller.config.multi_model_collaboration_enabled, true);
      assert.equal(isMultiModelCollaborationEnabled(), true);
      await act(async () => {
        await globalThis.collaborationFixture.controller.reload();
      });
      assert.equal(loads, 3);
      assert.equal(isMultiModelCollaborationEnabled(), false);
      assert.equal(globalThis.collaborationFixture.controller.config.multi_model_collaboration_enabled, false);
    },
  );
});

test('shared JEV key save and clear survive stale config reloads', async () => {
  let resolveSave;
  let resolveReload;
  let loads = 0;
  await mount(
    {
      probe: true,
      load: () => {
        loads += 1;
        if (loads === 2)
          return new Promise((resolve) => {
            resolveReload = resolve;
          });
        return {
          jev_typesafe_api_key: '',
          moa_jev_enabled: false,
        };
      },
      save: () =>
        new Promise((resolve) => {
          resolveSave = resolve;
        }),
    },
    async ({ services, render }) => {
      assert.equal(globalThis.collaborationFixture.controller.config.jev_typesafe_api_key, '');
      let saving;
      await act(async () => {
        saving = globalThis.collaborationFixture.controller.save(
          { jev_typesafe_api_key: 'new-test-key', moa_jev_enabled: 'true' },
          'jev-key',
        );
      });
      services.isConnected = false;
      await render();
      services.isConnected = true;
      await render();
      await act(async () => {
        resolveSave();
        await saving;
      });
      await act(async () => resolveReload({ jev_typesafe_api_key: '', moa_jev_enabled: false }));
      const config = globalThis.collaborationFixture.controller.config;
      assert.equal(config.jev_typesafe_api_key, 'new-test-key');
      assert.equal(config.moa_jev_enabled, 'true');
      assert.equal('moa_jev_api_key' in config, false);
      let clearing;
      await act(async () => {
        clearing = globalThis.collaborationFixture.controller.save(
          { jev_typesafe_api_key: '', moa_jev_enabled: 'false' }, 'clear-key',
        );
      });
      await act(async () => {
        resolveSave();
        await clearing;
      });
      const cleared = globalThis.collaborationFixture.controller.config;
      assert.equal(cleared.jev_typesafe_api_key, '');
      assert.equal(cleared.moa_jev_enabled, 'false');
      assert.equal('moa_jev_api_key_clear' in cleared, false);
    },
  );
});

test('config notifications refresh collaboration controls after shared config changes', async () => {
  let loads = 0;
  await mount(
    {
      load: () => {
        loads += 1;
        return { ...configured, moa_verifier_interval: loads === 1 ? '3' : '7' };
      },
    },
    async ({ get, dom }) => {
      await act(async () => dom.window.dispatchEvent(new dom.window.Event('jiuwen:settings-config-changed')));
      assert.equal(loads, 2);
      assert.equal(get('settings-collaboration-interval-input').value, '7');
      assert.equal(get('settings-collaboration-jev-configure-btn'), null);
      assert.equal(get('settings-source-loading'), null);
    },
  );
});

test('shared feature revision rejects snapshots read before another writer confirmed a flag change', () => {
  setMultiModelCollaborationEnabled(false);
  const before = getMultiModelCollaborationRevision();
  setMultiModelCollaborationEnabled(true);
  assert.equal(getMultiModelCollaborationRevision(), before + 1);
  assert.equal(applyMultiModelCollaborationSnapshot(false, before), false);
  assert.equal(isMultiModelCollaborationEnabled(), true);
  const latest = getMultiModelCollaborationRevision();
  assert.equal(applyMultiModelCollaborationSnapshot(false, latest), true);
  assert.equal(isMultiModelCollaborationEnabled(), false);
  const confirmed = getMultiModelCollaborationRevision();
  setMultiModelCollaborationEnabled(false);
  assert.equal(getMultiModelCollaborationRevision(), confirmed);
});

test('settings read cannot override a flag changed by a separate writer while its request was pending', async () => {
  let loads = 0;
  let resolveReload;
  await mount(
    {
      probe: true,
      load: () => {
        loads += 1;
        if (loads === 2)
          return new Promise((resolve) => {
            resolveReload = resolve;
          });
        return { multi_model_collaboration_enabled: false };
      },
    },
    async () => {
      let reloading;
      await act(async () => {
        reloading = globalThis.collaborationFixture.controller.reload();
      });
      setMultiModelCollaborationEnabled(true);
      await act(async () => {
        resolveReload({ multi_model_collaboration_enabled: false });
        await reloading;
      });
      assert.equal(isMultiModelCollaborationEnabled(), true);
      assert.equal(globalThis.collaborationFixture.controller.config.multi_model_collaboration_enabled, true);
      await act(async () => {
        await globalThis.collaborationFixture.controller.reload();
      });
      assert.equal(isMultiModelCollaborationEnabled(), false);
      assert.equal(globalThis.collaborationFixture.controller.config.multi_model_collaboration_enabled, false);
    },
  );
});

test('parent switch cannot discard an interval editor draft and unlocks after cancel or save', async () => {
  await mount({ config: enabled }, async ({ get, main, click, input, saves, unsavedChanges }) => {
    await click('settings-collaboration-interval-modify-btn');
    await input('settings-collaboration-interval-input', '7');
    assert.equal(main().disabled, true);
    await click('settings-switch', 'multi-model-collaboration-enabled');
    assert.equal(get('settings-collaboration-interval-input').value, '7');
    assert.equal(unsavedChanges.hasChanges(), true);
    assert.deepEqual(saves, []);
    await click('settings-collaboration-interval-cancel-btn');
    assert.equal(main().disabled, false);
    assert.equal(unsavedChanges.hasChanges(), false);
    await click('settings-collaboration-interval-modify-btn');
    await input('settings-collaboration-interval-input', '5');
    await click('settings-collaboration-interval-save-btn');
    assert.equal(main().disabled, false);
    assert.deepEqual(saves, [{ moa_verifier_interval: '5' }]);
  });
});
