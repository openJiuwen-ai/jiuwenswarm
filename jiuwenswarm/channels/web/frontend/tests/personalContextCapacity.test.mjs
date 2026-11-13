import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { readFile } from 'node:fs/promises';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' });
for (const name of ['window', 'document', 'navigator', 'Node', 'HTMLElement', 'localStorage']) {
  Object.defineProperty(globalThis, name, { configurable: true, value: name === 'window' ? dom.window : dom.window[name] });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
after(() => dom.window.close());
const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');
const { Simulate } = await import('react-dom/test-utils');
const { createInstance } = await import('i18next');
const { I18nextProvider } = await import('react-i18next');

await build({
  stdin: {
    contents: `
      export { SettingItemRenderer } from './src/features/settings/components/SettingItemRenderer';
      export { SettingsServicesProvider } from './src/features/settings/services/SettingsServicesProvider';
      export { personalContextCapacitySetting } from './src/features/settings/modules/personalContext/capacitySetting';
      export { usePersonalContextStore } from './src/stores/personalContextStore';
      export { pcApi } from './src/services/personalContextApi';
    `,
    resolveDir: process.cwd(), loader: 'ts',
  },
  outfile: 'node_modules/.cache/personal-context-capacity.mjs',
  bundle: true, platform: 'node', format: 'esm', packages: 'external',
  define: { 'import.meta.env': '{"DEV":false}' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
});
const { SettingItemRenderer, SettingsServicesProvider, personalContextCapacitySetting, usePersonalContextStore, pcApi } =
  await import('../node_modules/.cache/personal-context-capacity.mjs');
const get = (id) => document.querySelector(`[data-testid="${id}"]`);
const click = async (id) => act(async () => get(id).click());
const input = async (text) => act(async () => {
  const field = get('settings-inline-input-field');
  field.value = text;
  Simulate.change(field);
});

for (const language of ['zh', 'en']) {
  test(`${language} capacity input rejects invalid values and preserves saved value on failure`, async () => {
    const locale = JSON.parse(await readFile(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8'));
    const i18n = createInstance();
    await i18n.init({ lng: language, resources: { [language]: { translation: locale } } });
    const writes = [];
    let fail = false;
    const previousPatch = pcApi.patchConfig;
    const previousGetConfig = pcApi.getConfig;
    const previousGetStatus = pcApi.getStatus;
    pcApi.getConfig = async () => ({ ...usePersonalContextStore.getState().config });
    pcApi.getStatus = async () => ({ state: 'CONFIGURED' });
    pcApi.patchConfig = async (patch) => {
      writes.push(patch);
      if (fail) throw new Error('save failed');
      return { ...usePersonalContextStore.getState().config, ...patch };
    };
    usePersonalContextStore.setState({
      config: { ...usePersonalContextStore.getState().config, max_context_size_mb: 50 }, pendingWrites: {},
    });
    const root = createRoot(document.getElementById('root'));
    try {
      await act(async () => root.render(createElement(I18nextProvider, { i18n },
        createElement(SettingsServicesProvider, { isConnected: true, request: async () => ({}) },
          createElement(SettingItemRenderer, { item: personalContextCapacitySetting })))));
      assert.equal(get('settings-inline-input-field').value, '50');
      await click('settings-inline-input-modify-btn');
      for (const text of ['9', '10001', '10.5', 'abc', '', '1e2']) {
        await input(text);
        await click('settings-inline-input-save-btn');
        assert.equal(get('settings-inline-input-field').getAttribute('aria-invalid'), 'true');
        assert.equal(get('settings-inline-input-error').textContent, locale.settingsPanel.fields.max_context_size_mb.invalid);
      }
      assert.equal(writes.length, 0);
      for (const text of ['10', '10000']) {
        await input(text);
        await click('settings-inline-input-save-btn');
        assert.deepEqual(writes.at(-1), { max_context_size_mb: Number(text) });
        assert.equal(get('settings-inline-input-field').value, text);
        assert.equal(get('settings-inline-input-error'), null);
        await click('settings-inline-input-modify-btn');
      }
      fail = true;
      await input('25');
      await click('settings-inline-input-save-btn');
      assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 10000);
      assert.equal(get('settings-inline-input-error').textContent, 'save failed');
      assert.equal(usePersonalContextStore.getState().pendingWrites.max_context_size_mb, false);
      await click('settings-inline-input-cancel-btn');
      assert.equal(get('settings-inline-input-field').value, '10000');
    } finally {
      await act(async () => root.unmount());
      pcApi.patchConfig = previousPatch;
      pcApi.getConfig = previousGetConfig;
      pcApi.getStatus = previousGetStatus;
    }
  });
}

test('PCS inline feedback does not change default input error behavior', async () => {
  const i18n = createInstance();
  await i18n.init({ lng: 'zh', resources: { zh: { translation: {} } } });
  usePersonalContextStore.setState({
    config: { ...usePersonalContextStore.getState().config, max_context_size_mb: 50 }, pendingWrites: {},
  });
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(I18nextProvider, { i18n },
      createElement(SettingsServicesProvider, { isConnected: true, request: async () => ({}) },
        createElement(SettingItemRenderer, { item: { ...personalContextCapacitySetting, errorDisplay: undefined } })))));
    await click('settings-inline-input-modify-btn');
    await input('9');
    await click('settings-inline-input-save-btn');
    assert.equal(Boolean(get('settings-inline-input-error')), false);
    assert.notEqual(get('settings-inline-input-field').getAttribute('aria-invalid'), 'true');
    assert.equal(get('settings-inline-input-field').value, '9');
    assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 50);
  } finally {
    await act(async () => root.unmount());
  }
});

const initialState = usePersonalContextStore.getState();

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((accept, fail) => { resolve = accept; reject = fail; });
  return { promise, resolve, reject };
}

function prepareStore(context) {
  const previousApi = { ...pcApi };
  const config = { ...initialState.config, configured: true, strategy_profile: 'rules', model_index: 0,
    max_context_size_mb: 50, fetch_services: [] };
  usePersonalContextStore.setState({ ...initialState, config, pendingWrites: {} }, true);
  pcApi.getStatus = async () => ({ state: 'CONFIGURED' });
  pcApi.getConfig = async () => ({ ...usePersonalContextStore.getState().config });
  pcApi.listServices = async () => ({ services: [] });
  pcApi.getRunStatus = async () => ({ services: [] });
  context.after(() => {
    Object.assign(pcApi, previousApi);
    usePersonalContextStore.setState(initialState, true);
  });
  return config;
}

for (const operation of ['strategy', 'model']) {
  for (const outcome of ['success', 'failure']) {
    for (const capacityFirst of [true, false]) {
      test(`${operation} ${outcome} preserves capacity when ${capacityFirst ? 'capacity' : 'other write'} completes first`, async (context) => {
        const config = prepareStore(context);
        const other = deferred();
        const capacity = deferred();
        pcApi.patchConfig = (patch) => 'max_context_size_mb' in patch ? capacity.promise : other.promise;
        pcApi.selectModel = () => other.promise;
        const otherWrite = operation === 'strategy'
          ? usePersonalContextStore.getState().setStrategyProfile('balanced')
          : usePersonalContextStore.getState().selectModel(2);
        const otherResult = otherWrite.then(() => null, (error) => error);
        const capacityWrite = usePersonalContextStore.getState().setMaxContextSize(25);
        const finishOther = async () => {
          if (outcome === 'failure') other.reject(new Error('other write failed'));
          else other.resolve({ ...config, ...(operation === 'strategy' ? { strategy_profile: 'balanced' } : { model_index: 2 }) });
          const result = await otherResult;
          if (outcome === 'failure') assert.match(result.message, /other write failed/);
          else assert.equal(result, null);
        };
        if (!capacityFirst) await finishOther();
        capacity.resolve({ ...config, max_context_size_mb: 25 });
        await capacityWrite;
        if (capacityFirst) await finishOther();
        const final = usePersonalContextStore.getState();
        assert.equal(final.config.max_context_size_mb, 25);
        assert.equal(final.config.strategy_profile, operation === 'strategy' && outcome === 'success' ? 'balanced' : 'rules');
        assert.equal(final.config.model_index, operation === 'model' && outcome === 'success' ? 2 : 0);
        assert.equal(final.pendingWrites.max_context_size_mb, false);
      });
    }
  }
}

for (const read of ['loadConfig', 'batchRefresh']) {
  test(`${read} preserves a capacity save after an old GET but accepts a fresh GET`, async (context) => {
    const config = prepareStore(context);
    const oldGet = deferred();
    pcApi.getConfig = () => oldGet.promise;
    pcApi.patchConfig = async (patch) => ({ ...config, ...patch });
    if (read === 'batchRefresh') usePersonalContextStore.setState({ configNeedsReconciliation: true });
    const pending = usePersonalContextStore.getState()[read]();
    await usePersonalContextStore.getState().setMaxContextSize(25);
    oldGet.resolve({ ...config, strategy_profile: 'balanced' });
    await pending;
    assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 25);
    assert.equal(usePersonalContextStore.getState().config.strategy_profile, 'balanced');
    pcApi.getConfig = async () => ({ ...config, max_context_size_mb: 30 });
    if (read === 'batchRefresh') usePersonalContextStore.setState({ configNeedsReconciliation: true });
    await usePersonalContextStore.getState()[read]();
    assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 30);
  });
}

test('successive ordinary GET responses can update capacity without a local save', async (context) => {
  const config = prepareStore(context);
  const first = deferred();
  const second = deferred();
  let reads = 0;
  pcApi.getConfig = () => (++reads === 1 ? first.promise : second.promise);
  const firstRead = usePersonalContextStore.getState().loadConfig();
  const secondRead = usePersonalContextStore.getState().loadConfig();
  first.resolve({ ...config, max_context_size_mb: 25 });
  await firstRead;
  second.resolve({ ...config, max_context_size_mb: 30 });
  await secondRead;
  assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 30);
});

test('a failed capacity save does not block an authoritative GET response', async (context) => {
  const config = prepareStore(context);
  const pendingGet = deferred();
  pcApi.getConfig = () => pendingGet.promise;
  pcApi.patchConfig = async () => { throw new Error('capacity save failed'); };
  const pending = usePersonalContextStore.getState().loadConfig();
  await assert.rejects(usePersonalContextStore.getState().setMaxContextSize(25), /capacity save failed/);
  pendingGet.resolve({ ...config, max_context_size_mb: 30 });
  await pending;
  assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 30);
});

for (const errorCode of ['REQUEST_TIMEOUT', 'WS_CLOSED', 'BUSY']) {
  test(`${errorCode} reconciles an uncertain capacity save without changing other settings`, async (context) => {
    const config = prepareStore(context);
    const verified = deferred();
    let saved = config;
    pcApi.patchConfig = async (patch) => {
      saved = { ...saved, ...patch };
      throw Object.assign(new Error('response lost'), { code: errorCode });
    };
    pcApi.getConfig = () => verified.promise;
    await assert.rejects(usePersonalContextStore.getState().setMaxContextSize(25), /response lost/);
    assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, true);
    assert.equal(usePersonalContextStore.getState().pendingWrites.max_context_size_mb, false);
    const refresh = usePersonalContextStore.getState().batchRefresh();
    verified.resolve(saved);
    await refresh;
    assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 25);
    assert.equal(usePersonalContextStore.getState().config.model_index, 0);
    assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  });
}

test('an older configuration write cannot undo a capacity value recovered after a lost response', async (context) => {
  const config = prepareStore(context);
  const oldResponse = deferred();
  const recovered = deferred();
  pcApi.patchConfig = (patch) => 'max_context_size_mb' in patch
    ? Promise.reject(new Error('response lost')) : oldResponse.promise;
  pcApi.getConfig = () => recovered.promise;
  const strategy = usePersonalContextStore.getState().setStrategyProfile('balanced');
  await assert.rejects(usePersonalContextStore.getState().setMaxContextSize(25), /response lost/);
  const refresh = usePersonalContextStore.getState().batchRefresh();
  recovered.resolve({ ...config, strategy_profile: 'balanced', max_context_size_mb: 25 });
  await refresh;
  oldResponse.resolve({ ...config, strategy_profile: 'balanced' });
  await strategy;
  assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 25);
  pcApi.patchConfig = async (patch) => ({ ...config, ...patch });
  await usePersonalContextStore.getState().setMaxContextSize(30);
  assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 30);
});

for (const read of ['loadConfig', 'batchRefresh']) {
  test(`${read} started before an uncertain capacity save cannot end its reconciliation`, async (context) => {
    const config = prepareStore(context);
    const older = deferred();
    usePersonalContextStore.setState({ configNeedsReconciliation: true });
    pcApi.getConfig = () => older.promise;
    const pending = usePersonalContextStore.getState()[read]();
    pcApi.patchConfig = async () => { throw new Error('response lost'); };
    await assert.rejects(usePersonalContextStore.getState().setMaxContextSize(25), /response lost/);
    older.resolve(config);
    await pending;
    assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, true);
    pcApi.getConfig = async () => ({ ...config, max_context_size_mb: 25 });
    await usePersonalContextStore.getState().batchRefresh();
    assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 25);
    assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  });
}

for (const operation of ['setEnabled', 'setMasterEnabled']) {
  test(`${operation} cannot clear an unresolved capacity reconciliation`, async (context) => {
    const config = prepareStore(context);
    const olderStatus = deferred();
    const reconcileRead = deferred();
    let statusReads = 0;
    pcApi.startRuntime = async () => config;
    pcApi.setMasterEnabled = async () => config;
    pcApi.getStatus = () => ++statusReads === 1 ? olderStatus.promise : Promise.resolve({ state: 'CONFIGURED' });
    pcApi.getConfig = () => { reconcileRead.resolve(); return Promise.resolve({ ...config, max_context_size_mb: 25 }); };
    pcApi.patchConfig = async () => { throw new Error('response lost'); };
    const switching = usePersonalContextStore.getState()[operation](true);
    await Promise.resolve();
    await assert.rejects(usePersonalContextStore.getState().setMaxContextSize(25), /response lost/);
    await reconcileRead.promise;
    await Promise.resolve();
    olderStatus.resolve({ state: 'CONFIGURED' });
    await switching;
    assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, true);
    await usePersonalContextStore.getState().batchRefresh();
    assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 25);
    assert.equal(usePersonalContextStore.getState().configNeedsReconciliation, false);
  });
}

test('saving capacity back to its original value still protects it from an older GET', async (context) => {
  const config = prepareStore(context);
  const oldGet = deferred();
  pcApi.getConfig = () => oldGet.promise;
  pcApi.patchConfig = async (patch) => ({ ...config, ...patch });
  const pending = usePersonalContextStore.getState().loadConfig();
  await usePersonalContextStore.getState().setMaxContextSize(25);
  await usePersonalContextStore.getState().setMaxContextSize(50);
  oldGet.resolve({ ...config, max_context_size_mb: 10 });
  await pending;
  assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 50);
});

for (const operation of ['setEnabled', 'setMasterEnabled', 'setAgentUseEnabled']) {
  test(`${operation} keeps a capacity save completed while status is pending`, async (context) => {
    const config = prepareStore(context);
    const status = deferred();
    const statusRequested = deferred();
    pcApi.startRuntime = async () => ({ ...config, collection_enabled: true });
    pcApi.setMasterEnabled = async () => ({ ...config, master_enabled: true, collection_enabled: true, agent_use_enabled: true });
    pcApi.startAgentUse = async () => ({ ...config, agent_use_enabled: true });
    pcApi.patchConfig = async (patch) => ({ ...config, ...patch });
    pcApi.getStatus = () => { statusRequested.resolve(); return status.promise; };
    const pending = usePersonalContextStore.getState()[operation](true);
    await statusRequested.promise;
    await usePersonalContextStore.getState().setMaxContextSize(25);
    status.resolve({ state: 'CONFIGURED' });
    await pending;
    assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 25);
  });
}

for (const operation of ['setAgentUseEnabled', 'setServiceEnabled']) {
  test(`${operation} rollback keeps a capacity save completed during its request`, async (context) => {
    const config = prepareStore(context);
    const failed = deferred();
    pcApi.startAgentUse = () => failed.promise;
    pcApi.startService = () => failed.promise;
    pcApi.patchConfig = async (patch) => ({ ...config, ...patch });
    const pending = operation === 'setAgentUseEnabled'
      ? usePersonalContextStore.getState()[operation](true)
      : usePersonalContextStore.getState()[operation]('sample', true);
    const result = pending.catch((error) => error);
    await usePersonalContextStore.getState().setMaxContextSize(25);
    failed.reject(new Error('write failed'));
    assert.match((await result).message, /write failed/);
    assert.equal(usePersonalContextStore.getState().config.max_context_size_mb, 25);
  });
}
