import assert from 'node:assert/strict';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { build } from 'esbuild';

const frontendRoot = fileURLToPath(new URL('../', import.meta.url));
const output = fileURLToPath(
  new URL('../node_modules/.cache/personal-context-graph-status/GraphPanel.mjs', import.meta.url),
);

await build({
  entryPoints: [fileURLToPath(new URL('../src/components/PersonalContext/GraphPanel.tsx', import.meta.url))],
  outfile: output,
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  loader: { '.css': 'empty' },
  plugins: [
    {
      name: 'isolate-graph-status-inputs',
      setup(builder) {
        builder.onResolve({ filter: /^\.\.\/\.\.\/stores$/ }, () => ({
          path: 'stores',
          namespace: 'graph-status-test',
        }));
        builder.onResolve({ filter: /^\.\.\/\.\.\/services\/personalContextApi$/ }, () => ({
          path: 'api',
          namespace: 'graph-status-test',
        }));
        builder.onResolve({ filter: /^\.\.\/MarkdownRenderer$/ }, () => ({
          path: 'markdown',
          namespace: 'graph-status-test',
        }));
        builder.onResolve({ filter: /^react-i18next$/ }, () => ({ path: 'i18n', namespace: 'graph-status-test' }));
        builder.onLoad({ filter: /.*/, namespace: 'graph-status-test' }, ({ path }) => ({
          contents: {
            stores: 'export const usePersonalContextStore = () => globalThis.__pcGraphStatusStore;',
            api: 'export const pcApi = {}; export const PROVIDER_LABEL_KEYS = {}; export const isFetchStopTimeoutError = () => false;',
            markdown: 'export const MarkdownRenderer = () => null;',
            i18n: 'const t = (key) => key; export const useTranslation = () => ({ t });',
          }[path],
          loader: 'js',
        }));
      },
    },
  ],
  absWorkingDir: frontendRoot,
});

const { PersonalContextGraphPanel } = await import(
  new URL('../node_modules/.cache/personal-context-graph-status/GraphPanel.mjs', import.meta.url)
);

function runProgress(serviceId, runState) {
  return {
    service_id: serviceId,
    run_state: runState,
    progress_percent: runState === 'succeeded' ? 100 : 0,
    total_items: 0,
    completed_items: 0,
    last_error: null,
  };
}

async function renderEmptyHint(runState) {
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
  globalThis.__pcGraphStatusStore = {
    graph: { context_ready: false, nodes: [], edges: [] },
    loadingGraph: false,
    status: {
      configured: true,
      collection_enabled: true,
      agent_use_enabled: false,
      state: 'RUNNING',
      pipeline_running: true,
      pipeline_queue_size: 0,
      fetch_service_states: { notes: 'STOPPED', other: 'RUNNING' },
      fetch_service_errors: { notes: null, other: null },
      fetch_run_progress: {
        notes: runProgress('notes', runState),
        other: runProgress('other', 'succeeded'),
      },
      context_root: 'test-context',
      context_ready: false,
      last_error: null,
    },
    config: {
      configured: true,
      collection_enabled: true,
      agent_use_enabled: false,
      strategy_profile: 'rules',
      model_index: null,
      model_id: null,
      fetch_services: [
        { service_id: 'notes', enabled: false },
        { service_id: 'other', enabled: true },
      ],
    },
    loadGraph: async () => {},
    loadStatus: async () => {},
  };
  const root = createRoot(dom.window.document.getElementById('root'));
  try {
    await act(async () =>
      root.render(
        createElement(PersonalContextGraphPanel, {
          isConnected: false,
          isActive: false,
          onNavigateServices: () => {},
        }),
      ),
    );
    return dom.window.document.querySelector('[data-testid="personal-context-graph-empty-hint-canvas"]')?.textContent;
  } finally {
    await act(async () => root.unmount());
    delete globalThis.__pcGraphStatusStore;
    for (const [key, original] of originals) {
      if (original) Object.defineProperty(globalThis, key, original);
      else delete globalThis[key];
    }
    dom.window.close();
  }
}

test('idle scheduler is not shown as an active collection', async () => {
  assert.equal(await renderEmptyHint('succeeded'), 'personalContext.info.noGraph');
});

test('running fetch is shown as an active collection', async () => {
  assert.equal(await renderEmptyHint('running'), 'personalContext.info.collecting');
});

test('stopping fetch is still shown as an active collection', async () => {
  assert.equal(await renderEmptyHint('stopping'), 'personalContext.info.collecting');
});
