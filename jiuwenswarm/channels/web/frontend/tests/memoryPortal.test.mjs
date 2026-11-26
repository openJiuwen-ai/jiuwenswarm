import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import ts from 'typescript';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><body><div id="root"></div></body>', { url: 'http://localhost:6173' });
for (const name of ['window', 'document', 'navigator', 'HTMLElement']) {
  Object.defineProperty(globalThis, name, { configurable: true, value: name === 'window' ? dom.window : dom.window[name] });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');
await build({
  entryPoints: {
    page: 'src/features/memory/MemoryPage.tsx',
    state: 'src/features/memory/featureConfig.ts',
    settings: 'src/features/settings/modules/memory/definition.ts',
    config: 'src/features/settings/services/useSettingsConfig.ts',
  },
  outdir: 'node_modules/.cache/memory-portal', outExtension: { '.js': '.mjs' },
  bundle: true, splitting: true, packages: 'external', platform: 'node', format: 'esm', jsx: 'automatic',
  loader: { '.css': 'empty' },
  plugins: [{ name: 'boundaries', setup(builder) {
    builder.onResolve({ filter: /\/components\/ui$|\/assets\/settings$|^react-i18next$|SettingsServicesProvider$/ },
      (args) => ({ path: args.path, namespace: 'boundary' }));
    builder.onLoad({ filter: /.*/, namespace: 'boundary' }, ({ path }) => ({
      contents: path.endsWith('SettingsServicesProvider')
        ? 'export const useSettingsServices = () => globalThis.memorySettingsServices;'
        : path.endsWith('/assets/settings')
        ? 'export const settingsNavigationIcons = {memory: () => null};'
        : path === 'react-i18next'
          ? 'export const useTranslation = () => ({t: key => key});'
          : `import {createElement} from 'react';
             export const Button = props => createElement('button', props);
             export const Loading = props => createElement('span', props);`,
      loader: 'js', resolveDir: process.cwd(),
    }));
  } }],
});
const { MemoryPage } = await import('../node_modules/.cache/memory-portal/page.mjs');
const state = await import('../node_modules/.cache/memory-portal/state.mjs');
const { useSettingsConfig } = await import('../node_modules/.cache/memory-portal/config.mjs');
const { memoryModule } = await import('../node_modules/.cache/memory-portal/settings.mjs');

function MemoryHost({ request }) {
  return state.useMemoryEnabled() ? createElement(MemoryPage, { isConnected: true, request }) : null;
}

test('Portal switch unmounts the Portal and late links cannot reopen it', async () => {
  const pending = [];
  const request = (method) => {
    assert.equal(method, 'memory.portal.open');
    return new Promise((resolve) => pending.push(resolve));
  };
  const root = createRoot(document.getElementById('root'));
  const link = (name) => ({ status: 'ready', url: `http://127.0.0.1:1234/portal/#access=${name}` });
  const frame = () => document.querySelector('iframe');
  try {
    state.setMemoryEnabled(false);
    await act(async () => root.render(createElement(MemoryHost, { request })));
    assert.equal(pending.length, 0);
    await act(async () => state.setMemoryEnabled(true));
    assert.equal(pending.length, 1);
    await act(async () => state.setMemoryEnabled(false));
    await act(async () => pending[0](link('stale')));
    assert.equal(frame(), null);
    await act(async () => state.setMemoryEnabled(true));
    assert.equal(pending.length, 2);
    await act(async () => pending[1](link('current')));
    assert.equal(frame().src, link('current').url);
    assert.equal(frame().getAttribute('referrerpolicy'), 'no-referrer');
    await act(async () => document.querySelector('button').click());
    assert.equal(frame(), null);
    await act(async () => pending[2]({ status: 'unavailable', code: 'timeout' }));
    assert.match(document.querySelector('[role="status"]').textContent, /timeout$/);
    await act(async () => state.setMemoryEnabled(false));
    assert.equal(document.getElementById('root').textContent, '');
  } finally {
    await act(async () => root.unmount());
  }
});

test('a missing Portal setting keeps navigation disabled', () => {
  state.setMemoryEnabled(true);
  assert.equal(state.applyMemorySnapshot(undefined, state.getMemoryRevision()), true);
  assert.equal(state.isMemoryEnabled(), false);
});

test('a stale configuration read cannot override a confirmed switch save', () => {
  state.setMemoryEnabled(true);
  const oldRead = state.getMemoryRevision();
  state.setMemoryEnabled(false);
  assert.equal(state.applyMemorySnapshot(true, oldRead), false);
  assert.equal(state.isMemoryEnabled(), false);
  assert.equal(state.applyMemorySnapshot('true', state.getMemoryRevision()), true);
  assert.equal(state.isMemoryEnabled(), true);
});

test('Memory settings contain only the Portal switch and no embedded page', () => {
  assert.equal(memoryModule.sections.length, 1);
  assert.deepEqual(memoryModule.sections[0].items, [
    { id: 'memory-portal-enabled', component: 'switch', key: 'memory_portal_enabled' },
  ]);
});


test('another tab can disable memory and invalidate an outstanding snapshot', () => {
  state.setMemoryEnabled(true);
  const pending = state.getMemoryRevision();
  window.dispatchEvent(new window.StorageEvent('storage', {
    key: 'jiuwenswarm.memory.portal.switch', newValue: JSON.stringify({ enabled: false }),
  }));
  assert.equal(state.isMemoryEnabled(), false);
  assert.equal(state.applyMemorySnapshot(true, pending), false);
  window.dispatchEvent(new window.StorageEvent('storage', {
    key: 'jiuwenswarm.memory.portal.switch', newValue: '{invalid',
  }));
  assert.equal(state.isMemoryEnabled(), false);
});


test('another tab refreshes the visible settings value without saving it again', async () => {
  let enabled = true;
  let reads = 0;
  globalThis.memorySettingsServices = {
    isConnected: true,
    request: async (method) => {
      assert.equal(method, 'config.get');
      reads += 1;
      return { memory_portal_enabled: String(enabled) };
    },
  };
  function SettingsValue() {
    const { config } = useSettingsConfig();
    return createElement('output', null, String(config.memory_portal_enabled));
  }
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(SettingsValue)));
    assert.equal(document.querySelector('output').textContent, 'true');
    for (const next of [false, true]) {
      enabled = next;
      const previousReads = reads;
      await act(async () => window.dispatchEvent(new window.StorageEvent('storage', {
        key: 'jiuwenswarm.memory.portal.switch', newValue: JSON.stringify({ enabled }),
      })));
      assert.equal(state.isMemoryEnabled(), enabled);
      assert.equal(document.querySelector('output').textContent, String(enabled));
      assert.equal(reads, previousReads + 1);
    }
    const previousReads = reads;
    await act(async () => window.dispatchEvent(new window.StorageEvent('storage', {
      key: 'jiuwenswarm.memory.portal.switch', newValue: JSON.stringify({ enabled: 'false' }),
    })));
    assert.equal(reads, previousReads);
    assert.equal(document.querySelector('output').textContent, 'true');
  } finally {
    await act(async () => root.unmount());
    delete globalThis.memorySettingsServices;
  }
});


test('JiuwenMate memory navigation mounts the Portal and respects the switch', async () => {
  const source = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  const tree = ts.createSourceFile('App.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let callback;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'renderJiuwenMateExistingPage') {
      callback = node.initializer.getText(tree);
    }
    ts.forEachChild(node, visit);
  }
  visit(tree);
  assert.ok(callback, 'JiuwenMate page renderer must exist');
  const script = ts.transpile(`(${callback})`, { jsx: ts.JsxEmit.React, jsxFactory: 'createElement' });
  const renderer = Function('createElement', 'MemoryPage', 'memoryEnabled', 'isConnected', 'request', `return ${script}`);
  const url = 'http://127.0.0.1:1234/portal/#access=mate-test';
  const request = async (method) => {
    assert.equal(method, 'memory.portal.open');
    return { status: 'ready', url };
  };
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(renderer(createElement, MemoryPage, true, true, request)('memory')));
    assert.equal(document.querySelector('iframe').src, url);
    await act(async () => root.render(renderer(createElement, MemoryPage, false, true, request)('memory')));
    assert.equal(document.querySelector('iframe'), null);
  } finally {
    await act(async () => root.unmount());
  }
});
