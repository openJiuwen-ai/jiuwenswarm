import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><body><div id="root"></div></body>', {
  url: 'http://localhost/', pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'navigator', 'Node', 'HTMLElement', 'HTMLDialogElement', 'localStorage']) {
  Object.defineProperty(globalThis, name, { configurable: true, value: name === 'window' ? dom.window : dom.window[name] });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
globalThis.requestAnimationFrame = dom.window.requestAnimationFrame.bind(dom.window);
globalThis.cancelAnimationFrame = dom.window.cancelAnimationFrame.bind(dom.window);
dom.window.HTMLDialogElement.prototype.showModal = function () { this.open = true; };
dom.window.HTMLDialogElement.prototype.close = function () { this.open = false; };
after(() => dom.window.close());

const { act, createElement, Fragment } = await import('react');
const { createRoot } = await import('react-dom/client');
const { createInstance } = await import('i18next');
const { I18nextProvider } = await import('react-i18next');
const resources = {};
for (const language of ['zh', 'en']) {
  resources[language] = { translation: JSON.parse(await readFile(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')) };
}

await build({
  stdin: {
    contents: `
      export { SettingsPageLayout } from './src/features/settings/SettingsPageLayout';
      export { SettingsServicesProvider, useSettingsServices } from './src/features/settings/services/SettingsServicesProvider';
      export { browserModule } from './src/features/settings/modules/browser/definition';
    `,
    resolveDir: process.cwd(), loader: 'ts',
  },
  outfile: 'node_modules/.cache/browser-settings-feedback.mjs',
  bundle: true, platform: 'node', format: 'esm', packages: 'external',
  define: { 'import.meta.env': '{"DEV":false}' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [{
    name: 'settings-icons',
    setup(builder) {
      builder.onResolve({ filter: /assets\/settings$/ }, () => ({ path: 'settings-icons', namespace: 'test-icons' }));
      builder.onLoad({ filter: /.*/, namespace: 'test-icons' }, () => ({
        contents: 'export const settingsNavigationIcons = { browser: () => null };', loader: 'js',
      }));
    },
  }],
});
const { SettingsPageLayout, SettingsServicesProvider, useSettingsServices, browserModule } = await import(
  '../node_modules/.cache/browser-settings-feedback.mjs'
);
const get = (id, variant) => document.querySelector(`[data-testid="${id}"]${variant ? `[data-variant="${variant}"]` : ''}`);
async function click(id, variant) {
  const element = get(id, variant);
  assert.ok(element, `Missing ${id}:${variant ?? ''}`);
  await act(async () => element.click());
}
async function chooseMode(mode = 'hybrid') {
  await click('settings-select', 'browser-decision-mode');
  await click('ui-select-option', mode === 'hybrid' ? '2' : '1');
}

async function mount(run, { language = 'zh', keyEnv = 'TYPESAFE_API_KEY', save } = {}) {
  const i18n = createInstance();
  await i18n.init({ lng: language, fallbackLng: 'en', resources, interpolation: { escapeValue: false } });
  let services;
  let browser = { chrome_path: '', headless: true, decision_mode: 'llm', jev_key_configured: false };
  const writes = [];
  const request = async (method, params) => {
    if (method === 'path.get') return { ...browser };
    if (method === 'path.set') {
      writes.push(params);
      if (save) browser = { ...browser, ...await save(params) };
      else throw new Error(`Jev API key is not configured (${keyEnv})`);
      return { ...browser };
    }
    throw new Error(`Unexpected ${method}`);
  };
  function Capture() { services = useSettingsServices(); return null; }
  const definition = {
    id: 'feedback-test', compositionMode: 'base', accessPolicy: { evaluate: () => ({ level: 'editable' }) },
    modules: [browserModule, {
      id: 'models', titleKey: 'settingsPanel.categories.models', icon: () => null,
      sections: [{ id: 'models', items: [{ id: 'models', component: 'custom', render: () => null }] }],
    }],
  };
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(I18nextProvider, { i18n },
      createElement(SettingsServicesProvider, { request, isConnected: true, connectionState: 'ready' },
        createElement(Fragment, null, createElement(Capture), createElement(SettingsPageLayout, { definition })),
      ),
    )));
    await run({ i18n, services, writes });
  } finally {
    await act(async () => root.unmount());
    services.saveQueue.clear();
  }
}

for (const language of ['zh', 'en']) {
  for (const keyEnv of ['TYPESAFE_API_KEY', 'OPENROUTER_API_KEY', 'JEV_CUSTOM_API_KEY']) {
    for (const mode of ['hybrid', 'shadow']) {
      test(`${language}: missing ${keyEnv} in ${mode} is localized and discarded on navigation`, async () => {
        await mount(async ({ i18n, writes }) => {
          await chooseMode(mode);
          assert.equal(get('settings-save-status', 'error').textContent, i18n.t('settingsPanel.browser.jevKeyMissing'));
          assert.equal(get('settings-select', 'browser-decision-mode').textContent, i18n.t('settingsPanel.browser.decisionLlm'));
          assert.equal(writes.length, 1);
          await click('settings-nav-button', 'models');
          assert.equal(get('settings-save-status'), null);
          await click('settings-nav-button', 'browser');
          assert.equal(get('settings-save-status'), null);
          assert.equal(get('settings-select', 'browser-decision-mode').textContent, i18n.t('settingsPanel.browser.decisionLlm'));
        }, { language, keyEnv });
      });
    }
  }
}

test('shared-connection errors use the current UI language and do not hide unrelated errors', async () => {
  let message = 'Jev API key is required in Models > Jev settings';
  await mount(async ({ i18n }) => {
    await chooseMode();
    assert.equal(get('settings-save-status', 'error').textContent, resources.zh.translation.settingsPanel.browser.jevKeyMissing);
    await act(async () => i18n.changeLanguage('en'));
    assert.equal(get('settings-save-status', 'error').textContent, resources.en.translation.settingsPanel.browser.jevKeyMissing);
    message = 'Connection refused';
    await chooseMode();
    assert.equal(get('settings-save-status', 'error').textContent, message);
  }, { save: async () => { throw new Error(message); } });
});

test('cancelled navigation keeps the error; confirmed draft discard clears it', async () => {
  await mount(async ({ services, i18n }) => {
    await chooseMode();
    services.unsavedChanges.set('draft', true);
    await click('settings-nav-button', 'models');
    const dialogButton = text => [...document.querySelectorAll('dialog[open] button')].find(button => button.textContent === text);
    await act(async () => dialogButton(i18n.t('common.cancel')).click());
    assert.ok(get('settings-save-status', 'error'));
    await click('settings-nav-button', 'models');
    await act(async () => dialogButton(i18n.t('common.confirm')).click());
    assert.ok(get('settings-module', 'models'));
    assert.equal(get('settings-save-status'), null);
    services.unsavedChanges.clear('draft');
    await click('settings-nav-button', 'browser');
    assert.equal(get('settings-save-status'), null);
  });
});

test('pending saves still block navigation and valid Jev settings can be saved', async () => {
  let resolve;
  await mount(async () => {
    await chooseMode();
    assert.equal(get('settings-nav-button', 'models').disabled, true);
    await click('settings-nav-button', 'models');
    assert.ok(get('settings-module', 'browser'));
    await act(async () => resolve());
    assert.ok(get('settings-save-status', 'saved'));
    await click('settings-nav-button', 'models');
    assert.equal(get('settings-save-status'), null);
    await click('settings-nav-button', 'browser');
    assert.equal(get('settings-select', 'browser-decision-mode').textContent, resources.zh.translation.settingsPanel.browser.decisionHybrid);
  }, { save: params => new Promise(done => { resolve = () => done(params); }) });
});
