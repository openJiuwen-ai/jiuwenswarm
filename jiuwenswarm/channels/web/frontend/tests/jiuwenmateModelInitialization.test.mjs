import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import * as esbuild from 'esbuild';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import i18n from 'i18next';
import { initReactI18next } from 'react-i18next';

await esbuild.build({
  entryPoints: ['src/features/jiuwenmate/JiuwenMateApp.tsx'],
  outfile: 'node_modules/.cache/jiuwenmate-model-initialization/App.mjs',
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl', '.webp': 'dataurl' },
  define: { 'import.meta.env.DEV': 'false', 'import.meta.glob': '__testAssetGlob' },
  banner: { js: 'var __testAssetGlob = () => ({});' },
  plugins: [{
    name: 'web-boundary',
    setup(build) {
      build.onResolve({ filter: /\/services\/webClient$/ }, () => ({ path: 'web', namespace: 'test-web' }));
      build.onLoad({ filter: /.*/, namespace: 'test-web' }, () => ({
        contents: `export const webClient = globalThis.__mateWebClient;
          export const webRequest = globalThis.__mateWebRequest;
          export const requestAgentDetail = (...args) => webRequest('agent.detail', ...args);
          export const requestPhaseAgents = (...args) => webRequest('agents.list', ...args);
          export const requestGoalAction = (...args) => webRequest('goal.action', ...args);
          export const sendGoalStreamCommand = (...args) => webRequest('goal.stream', ...args);`,
      }));
      build.onResolve({ filter: /\.svg\?react$/ }, (args) => ({ path: args.path, namespace: 'test-svg' }));
      build.onLoad({ filter: /.*/, namespace: 'test-svg' }, () => ({
        contents: 'export default function Svg() { return null; }',
      }));
    },
  }],
});

test('an older employee load cannot overwrite the model initialization refresh', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = new Map();
  const globals = {
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    localStorage: dom.window.localStorage, MutationObserver: dom.window.MutationObserver,
    ResizeObserver: class { observe() {} disconnect() {} }, IS_REACT_ACT_ENVIRONMENT: true,
  };
  for (const [key, value] of Object.entries(globals)) {
    previous.set(key, Object.getOwnPropertyDescriptor(globalThis, key));
    Object.defineProperty(globalThis, key, { configurable: true, writable: true, value });
  }
  const handlers = new Map();
  globalThis.__mateWebClient = {
    getState: () => 'ready',
    onStateChange: () => () => {},
    on(event, handler) {
      handlers.set(event, handler);
      return () => handlers.delete(event);
    },
  };
  let generation = 0;
  let lists = 0;
  let bindingReads = 0;
  let releaseOldBindings;
  const oldBindings = new Promise((resolve) => { releaseOldBindings = resolve; });
  const employee = (name, model, version) => ({
    agent_id: 'default-employee', name, description: '', tags: [], category: 'custom',
    capabilities: { model, skills: [], connectors: [], plugins: [], knowledge: [], starter_prompts: [] },
    is_default: true, enabled: true, current_version: version, has_unpublished_changes: version === null,
    created_at: '2026-11-03T00:00:00Z', updated_at: '2026-11-03T00:00:00Z', connections: [],
  });
  globalThis.__mateWebRequest = async (method) => {
    if (method === 'models.refresh_status') return { status: 'applied', generation };
    if (method === 'jiuwenmate.colleague.list') {
      lists += 1;
      return { colleagues: [lists === 1
        ? employee('初始化前员工', null, null)
        : employee('新模型员工', 'real-model#0', 'v1')] };
    }
    if (method === 'channel.welink.get_bindings') return ++bindingReads === 1 ? oldBindings : { bindings: [] };
    if (method === 'jiuwenmate.models.list') return { models: [], current_selection: '', current_display_name: '' };
    return { groups: [], total: 0 };
  };
  const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
  await i18n.use(initReactI18next).init({ lng: 'zh', resources: { zh: { translation: zh } } });
  const { createRoot } = await import('react-dom/client');
  const { JiuwenMateApp } = await import('../node_modules/.cache/jiuwenmate-model-initialization/App.mjs');
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  try {
    await act(async () => root.render(React.createElement(JiuwenMateApp, { onExit() {}, renderExistingPage: () => null })));
    assert.equal(lists, 1);
    generation = 1;
    await act(async () => handlers.get('models.updated')({ payload: {} }));
    assert.equal(lists, 2);
    assert.match(container.textContent, /新模型员工/);
    await act(async () => releaseOldBindings({ bindings: [] }));
    assert.match(container.textContent, /新模型员工/);
    assert.doesNotMatch(container.textContent, /初始化前员工/);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    delete globalThis.__mateWebClient;
    delete globalThis.__mateWebRequest;
    for (const [key, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, key, descriptor);
      else delete globalThis[key];
    }
  }
});
