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

async function renderStatus({ runState, completed = 0, failed = 0, pending = false }) {
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
  };
  globalThis.__pcServiceStore = {
    config: {
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
      fetch_service_states: { notes: 'STOPPED' },
      fetch_service_errors: { notes: null },
      fetch_run_progress: { notes: run },
    },
    runHistories: { notes: [run] },
    loadingServices: false,
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
          isConnected: false,
          isActive: false,
          onBackToGraph: () => {},
        }),
      ),
    );
    return {
      card: dom.window.document.querySelector('.pc-services__status-text')?.textContent,
      activeCount: dom.window.document.querySelector('.pc-services__stat-card:last-child .pc-services__stat-number')?.textContent,
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
