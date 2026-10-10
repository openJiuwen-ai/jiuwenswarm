import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { build } from 'esbuild';

const frontendRoot = fileURLToPath(new URL('../', import.meta.url));
const output = fileURLToPath(
  new URL('../node_modules/.cache/personal-context-run-state/ServicesPanel.mjs', import.meta.url),
);

await build({
  entryPoints: [fileURLToPath(new URL('../src/components/PersonalContext/ServicesPanel.tsx', import.meta.url))],
  outfile: output,
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  loader: { '.css': 'empty' },
  plugins: [
    {
      name: 'isolate-services-panel-dependencies',
      setup(builder) {
        const dependencies = new Map([
          ['../../stores', 'export const usePersonalContextStore = () => globalThis.__pcServiceStore;'],
          [
            '../../services/personalContextApi',
            `
          export const PROVIDER_ORDER = ['local_files'];
          export const PROVIDER_LABEL_KEYS = { local_files: 'local' };
          export const FREQUENCY_SECONDS = { hour: 3600, day: 86400 };
          export const hasRunningFetchTask = () => false;
          export const isFetchTaskRunningError = () => false;
        `,
          ],
          ['../../features/settings/settingsNavigation', 'export const requestSettingsModule = () => {};'],
          ['../../components/ui/Toast/toastStore', 'export const toast = { open() {} };'],
          ['../Switch', 'export const Switch = () => null;'],
          ['./AddContentDrawer', 'export const AddContentDrawer = () => null;'],
          [
            'react-i18next',
            `
          export const useTranslation = () => ({
            t: (key, vars) => vars ? key + ':' + Object.values(vars).join('/') : key,
          });
        `,
          ],
        ]);
        builder.onResolve({ filter: /\.(svg|png)$/ }, () => ({ path: 'asset', namespace: 'pc-run-state-test' }));
        builder.onResolve({ filter: /.*/ }, (args) =>
          dependencies.has(args.path) ? { path: args.path, namespace: 'pc-run-state-test' } : null,
        );
        builder.onLoad({ filter: /.*/, namespace: 'pc-run-state-test' }, (args) => ({
          contents: args.path === 'asset' ? 'export default "";' : dependencies.get(args.path),
          loader: 'js',
        }));
      },
    },
  ],
  absWorkingDir: frontendRoot,
});

const { PersonalContextServicesPanel } = await import(
  new URL('../node_modules/.cache/personal-context-run-state/ServicesPanel.mjs', import.meta.url)
);

async function renderStatus({ runState, completed = 0, failed = 0, pending = false, result = {}, config = {}, runtime = 'RUNNING', reconciling = false, serviceState = 'STOPPED', currentRun }) {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
  const globals = {
    window: dom.window,
    document: dom.window.document,
    HTMLElement: dom.window.HTMLElement,
    IS_REACT_ACT_ENVIRONMENT: true,
  };
  const originals = new Map(Object.keys(globals).map((key) => [key, Object.getOwnPropertyDescriptor(globalThis, key)]));
  for (const [key, value] of Object.entries(globals)) {
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const run = {
    service_id: 'notes',
    run_id: 'run-1',
    run_state: runState,
    started_at: '2026-09-23T00:00:00Z',
    finished_at: '2026-09-23T00:00:01Z',
    progress_percent: 100,
    total_items: completed + failed,
    completed_items: completed,
    failed_items: failed,
    quarantined_items: failed,
    item_errors: [],
    omitted_item_errors: 0,
    last_error: runState === 'failed' ? 'failed to fetch' : null,
    ...result,
  };
  globalThis.__pcServiceStore = {
    config: {
      master_enabled: true,
      collection_enabled: true,
      ...config,
      fetch_services: [
        {
          service_id: 'notes',
          provider: 'local_files',
          enabled: false,
          interval_seconds: 3600,
        },
      ],
    },
    graph: { nodes: [] },
    status: {
      state: runtime,
      fetch_service_states: { notes: serviceState },
      fetch_service_errors: { notes: null },
      fetch_run_progress: { notes: currentRun ?? run },
    },
    runHistories: { notes: [run] },
    loadingServices: false,
    configNeedsReconciliation: reconciling,
    pendingWrites: pending ? { 'run:notes': true } : {},
    batchRefresh: async () => {},
    setServiceEnabled: async () => {},
    deleteService: async () => {},
    runOne: async () => {},
    stopRun: async () => {},
    authByProvider: {},
    loadAuthStatus: async () => {},
    isProviderAuthorized: () => true,
  };
  const root = createRoot(dom.window.document.getElementById('root'));
  try {
    await act(async () =>
      root.render(
        createElement(PersonalContextServicesPanel, {
          isConnected: true,
          isActive: false,
          onBackToGraph: () => {},
        }),
      ),
    );
    return {
      card: dom.window.document.querySelector('.pc-services__status-text')?.textContent,
      activeCount: dom.window.document.querySelector('.pc-services__stat-card:last-child .pc-services__stat-number')?.textContent,
      result: dom.window.document.querySelector('[data-testid="personal-context-service-result"]')?.textContent,
      runDisabled: [...dom.window.document.querySelectorAll('button')].find((button) => button.textContent === 'personalContext.services.actionRunNow')?.disabled,
      runHint: dom.window.document.querySelector('[data-testid="personal-context-service-run-hint"]')?.textContent,
    };
  } finally {
    await act(async () => root.unmount());
    delete globalThis.__pcServiceStore;
    for (const [key, original] of originals) {
      if (original) Object.defineProperty(globalThis, key, original);
      else delete globalThis[key];
    }
    dom.window.close();
  }
}

test('a new request does not display the previous completed run', async () => {
  assert.equal(
    (await renderStatus({ runState: 'succeeded', pending: true })).card,
    'personalContext.services.stateCollecting',
  );
});

test('a genuinely completed empty run still displays completion', async () => {
  assert.equal((await renderStatus({ runState: 'succeeded' })).card, 'personalContext.services.stateCompleted');
});

test('a partial run reports completed and failed item counts', async () => {
  assert.equal(
    (await renderStatus({ runState: 'partial_succeeded', completed: 1, failed: 1 })).card,
    'personalContext.services.statePartial:1/2/1',
  );
});

test('a system failure still displays failure', async () => {
  assert.equal((await renderStatus({ runState: 'failed', failed: 1 })).card, 'personalContext.services.stateFailed');
});

test('a stopping fetch remains visible in the active task count', async () => {
  assert.equal((await renderStatus({ runState: 'stopping' })).activeCount, '1');
});

test('a terminal fetch is absent from the active task count', async () => {
  assert.equal((await renderStatus({ runState: 'cancelled' })).activeCount, '0');
});

test('manual collection respects global switches and stopping while source automatic collection stays independent', async () => {
  assert.equal((await renderStatus({ runState: 'idle' })).runDisabled, false);
  for (const options of [
    { config: { master_enabled: false } },
    { config: { collection_enabled: false } },
    { runtime: 'STOPPING' },
    { serviceState: 'STOPPING' },
    { reconciling: true },
  ]) {
    const card = await renderStatus({ runState: 'idle', ...options });
    assert.equal(card.runDisabled, true, JSON.stringify(options));
    assert.ok(card.runHint, 'disabled manual collection needs an explanation');
  }
});

test('a completed run displays published nodes and files rather than fetched item counts', async () => {
  const card = await renderStatus({ runState: 'succeeded', completed: 1, result: { created_node_count: 4, updated_node_count: 0 } });
  assert.equal(card.result, 'personalContext.services.createdNodes:4/4');
});

test('no new content is explicit only for a successful unchanged run', async () => {
  const result = { created_node_count: 0, updated_node_count: 0, no_new_content: true };
  assert.equal((await renderStatus({ runState: 'succeeded', result })).result, 'personalContext.services.noNewContent');
  for (const runState of ['failed', 'partial_succeeded', 'cancelled']) {
    const card = await renderStatus({ runState, result });
    assert.notEqual(card.result, 'personalContext.services.noNewContent');
  }
});

test('updates are distinguished from unchanged content, and historical unknown counts stay unknown', async () => {
  const updated = await renderStatus({ runState: 'succeeded', result: { created_node_count: 0, updated_node_count: 3 } });
  assert.equal(updated.result, 'personalContext.services.createdNodes:0/0 · personalContext.services.updatedNodes:3');
  assert.equal((await renderStatus({ runState: 'succeeded', completed: 8 })).result, undefined);
});

test('partial and cancelled runs retain terminal state alongside committed results', async () => {
  for (const runState of ['partial_succeeded', 'cancelled']) {
    const card = await renderStatus({ runState, result: { created_node_count: 2, updated_node_count: 1 } });
    assert.ok(card.result?.includes('personalContext.services.createdNodes:2/2'));
    assert.ok(card.card.includes(runState === 'cancelled' ? 'stateCancelled' : 'statePartial'));
  }
  assert.equal((await renderStatus({ runState: 'succeeded', pending: true, result: { created_node_count: 2 } })).result, undefined);
});

test('a newer terminal progress never shows unchanged content or counts from older successful history', async () => {
  for (const [run_state, label] of [['failed', 'stateFailed'], ['partial_succeeded', 'statePartial'], ['cancelled', 'stateCancelled']]) {
    const card = await renderStatus({
      runState: 'succeeded',
      result: { created_node_count: 0, updated_node_count: 0, no_new_content: true },
      currentRun: { run_state, completed_items: 1, total_items: 2, failed_items: 1 },
    });
    assert.ok(card.card.includes(label), run_state);
    assert.equal(card.result, undefined, run_state);
  }
});

test('a newer terminal progress publishes its own results while history is unavailable', async () => {
  const card = await renderStatus({
    runState: 'succeeded',
    result: { created_node_count: 0, updated_node_count: 0, no_new_content: true },
    currentRun: { run_state: 'partial_succeeded', completed_items: 1, total_items: 2, failed_items: 1, created_node_count: 3, updated_node_count: 2 },
  });
  assert.equal(card.card, 'personalContext.services.statePartial:1/2/1');
  assert.equal(card.result, 'personalContext.services.createdNodes:3/3 · personalContext.services.updatedNodes:2');
});

test('a failed scheduler cannot display unchanged content from a historical successful run', async () => {
  const card = await renderStatus({ runState: 'succeeded', serviceState: 'FAILED', result: { no_new_content: true } });
  assert.equal(card.card, 'personalContext.services.stateFailed');
  assert.equal(card.result, undefined);
});
