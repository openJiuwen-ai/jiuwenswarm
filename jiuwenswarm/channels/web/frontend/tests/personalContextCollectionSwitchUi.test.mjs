import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { build } from 'esbuild';

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const output = path.join(frontend, 'node_modules/.cache/personal-context-4681/SettingsPanel.mjs');

const config = {
  configured: true,
  master_enabled: true,
  collection_enabled: true,
  agent_use_enabled: true,
  strategy_profile: 'rules',
  model_index: null,
  model_id: null,
  fetch_services: [],
};
let reconciling = true;
globalThis.__pcSettingsState = () => ({
  config,
  status: { configured: true },
  loadingConfig: false,
  pendingWrites: {},
  configNeedsReconciliation: reconciling,
  loadAll: async () => {},
  setMasterEnabled: async () => {},
  setEnabled: async () => {},
  setStrategyProfile: async () => {},
  selectModel: async () => {},
  loadAuthStatus: async () => {},
  authorizeProvider: async () => {},
  authByProvider: {},
});

await build({
  entryPoints: [path.join(frontend, 'src/components/PersonalContext/SettingsPanel.tsx')],
  outfile: output,
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [
    {
      name: 'settings-boundaries',
      setup(plugin) {
        plugin.onResolve({ filter: /^react-i18next$/ }, () => ({ path: 'i18n', namespace: 'test' }));
        plugin.onResolve({ filter: /\/stores$/ }, () => ({ path: 'stores', namespace: 'test' }));
        plugin.onResolve({ filter: /personalContextApi$/ }, () => ({ path: 'api', namespace: 'test' }));
        plugin.onResolve({ filter: /ModelPicker$/ }, () => ({ path: 'model-picker', namespace: 'test' }));
        plugin.onResolve({ filter: /Toast\/toastStore$/ }, () => ({ path: 'toast', namespace: 'test' }));
        plugin.onLoad({ filter: /.*/, namespace: 'test' }, (args) => ({
          contents: {
            i18n: 'export const useTranslation = () => ({ t: (key) => key });',
            stores:
              'export const usePersonalContextStore = globalThis.__pcSettingsState; export const useSessionStore = (selector) => selector({ availableModels: [] });',
            api: 'export const STRATEGY_OPTIONS = []; export const hasRunningFetchTask = () => false; export const isFetchTaskRunningError = () => false; export const pcApi = {};',
            'model-picker': 'export default function ModelPicker() { return null; }',
            toast: 'export const toast = { open() {} };',
          }[args.path],
        }));
      },
    },
  ],
});
const { PersonalContextSettingsPanel } = await import(pathToFileURL(output).href);

test('settings switches stay disabled and show verification state until Host config is confirmed', () => {
  reconciling = true;
  const pending = renderToStaticMarkup(createElement(PersonalContextSettingsPanel, { isConnected: true }));
  assert.ok(pending.includes('personalContext.settings.collectionReconciling'));
  const pendingSwitches = [...pending.matchAll(/<button[^>]*role="switch"[^>]*>/g)].map(([tag]) => tag);
  assert.ok(pendingSwitches[0]?.includes('disabled'), 'master switch should be disabled');
  assert.ok(pendingSwitches[1]?.includes('disabled'), 'collection switch should be disabled');

  reconciling = false;
  const settled = renderToStaticMarkup(createElement(PersonalContextSettingsPanel, { isConnected: true }));
  assert.ok(!settled.includes('personalContext.settings.collectionReconciling'));
  const settledSwitches = [...settled.matchAll(/<button[^>]*role="switch"[^>]*>/g)].map(([tag]) => tag);
  assert.ok(!settledSwitches[0]?.includes('disabled'));
  assert.ok(!settledSwitches[1]?.includes('disabled'));
});

test('verification messages are localized in Chinese and English', async () => {
  for (const language of ['zh', 'en']) {
    const locale = JSON.parse(await readFile(path.join(frontend, `src/i18n/locales/${language}.json`), 'utf8'));
    assert.ok(locale.personalContext.settings.collectionReconciling);
    assert.ok(locale.personalContext.settings.collectionTimeoutReconciling);
  }
});
