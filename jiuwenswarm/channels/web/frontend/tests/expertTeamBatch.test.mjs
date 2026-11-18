import test, { after, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import { build } from 'esbuild';
import { pathToFileURL } from 'node:url';
import path from 'node:path';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { readFileSync } from 'node:fs';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

await i18next.use(initReactI18next).init({
  lng: 'zh', fallbackLng: 'en', interpolation: { escapeValue: false },
  resources: Object.fromEntries(['zh', 'en'].map((language) => [language, {
    translation: JSON.parse(readFileSync(`src/i18n/locales/${language}.json`, 'utf8')),
  }])),
});

const out = path.resolve('node_modules/.cache/expert-team-batch/test.mjs');
await build({
  stdin: {
    contents: `export { useExpertTeamBatchStore } from './src/stores/expertTeamBatchStore';
      export { useTeamSelectorStore } from './src/stores/teamSelectorStore';
      export { ExpertTeamBatchPicker } from './src/components/ChatPanel/ExpertTeamBatchPicker';`,
    resolveDir: process.cwd(), loader: 'tsx',
  },
  bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic', outfile: out,
  plugins: [{ name: 'transport-boundary', setup(builder) {
    builder.onResolve({ filter: /^(react|zustand)$/, namespace: 'test-boundary' }, ({ path }) => ({ path, external: true }));
    builder.onResolve({ filter: /services\/webClient$/ }, () => ({ path: 'web', namespace: 'test-boundary' }));
    builder.onResolve({ filter: /stores\/teamSelectorStore$|^\.\/teamSelectorStore$/ }, () => ({ path: 'teams', namespace: 'test-boundary' }));
    builder.onResolve({ filter: /^\.\/chatStore$/ }, () => ({ path: 'chat', namespace: 'test-boundary' }));
    builder.onResolve({ filter: /^\.\/sessionStore$/ }, () => ({ path: 'session', namespace: 'test-boundary' }));
    builder.onResolve({ filter: /^\.\.\/ui$/ }, () => ({ path: 'button', namespace: 'test-boundary' }));
    builder.onLoad({ filter: /.*/, namespace: 'test-boundary' }, ({ path: name }) => ({
      loader: 'js', contents: {
        web: 'export const webRequest = (...args) => globalThis.batchRequest(...args);',
        teams: `import { create } from 'zustand'; export const useTeamSelectorStore = create(() => ({
          runtimes: {}, fetchTeams: async (id) => {
            globalThis.batchRefreshes.push(id);
            if (globalThis.batchRefreshError) throw globalThis.batchRefreshError;
            return [];
          }
        }));`,
        chat: 'export const useChatStore = { getState: () => ({ensureRuntime: () => {}, addMessage: (id, message) => globalThis.batchMessages.push({id, message})}) };',
        session: 'export const useSessionStore = { getState: () => ({runtimes: {}}) };',
        button: `import React from 'react'; export const Button = ({variant, ...props}) => React.createElement('button', props);`,
      }[name],
    }));
  } }],
});
const { useExpertTeamBatchStore: store, useTeamSelectorStore: selector, ExpertTeamBatchPicker: Picker } = await import(pathToFileURL(out));
const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost' });
const previous = { window: globalThis.window, document: globalThis.document };
globalThis.window = dom.window;
globalThis.document = dom.window.document;
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
after(() => { Object.assign(globalThis, previous); dom.window.close(); });
beforeEach(() => {
  store.setState({ runtimes: {} });
  selector.setState({ runtimes: {} });
  globalThis.batchMessages = [];
  globalThis.batchRefreshError = null;
  globalThis.batchRefreshes = [];
  globalThis.batchRequest = async () => ({ results: [] });
});

test('a failed list refresh preserves successful RPC results and retries only failed groups', async () => {
  for (const name of ['a', 'b', 'c']) store.getState().toggle('s', name);
  const results = [
    { agent_group_name: 'a', status: 'created', team_id: 'owner' },
    { agent_group_name: 'b', status: 'existing', team_id: 'b-team' },
    { agent_group_name: 'c', status: 'failed', error: 'disabled' },
  ];
  globalThis.batchRequest = async () => ({ results, owner_agent_group_name: 'a' });
  globalThis.batchRefreshError = new Error('team.list disconnected');
  const warn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    await store.getState().submit('s', async () => 's');
    assert.deepEqual(store.getState().runtimes.s.results, results);
    assert.deepEqual(store.getState().runtimes.s.selected, ['c']);
    assert.equal(store.getState().runtimes.s.error, null);
    assert.equal(store.getState().runtimes.s.submitting, false);
    assert.equal(store.getState().runtimes.s.ownerGroup, 'a');
    assert.equal(warnings.length, 1);
    globalThis.batchRefreshError = null;
    globalThis.batchRequest = async (_method, params) => {
      assert.deepEqual(params.agent_group_names, ['c']);
      return { results: [{ agent_group_name: 'c', status: 'created', team_id: 'c-team' }] };
    };
    await store.getState().submit('s', async () => 's');
    assert.deepEqual(store.getState().runtimes.s.selected, []);
    assert.equal(store.getState().runtimes.s.ownerGroup, 'a');
  } finally {
    console.warn = warn;
  }
});

test('the first selected group is previewed as owner and existing owners remain fixed', async () => {
  const root = createRoot(document.getElementById('root'));
  const groups = ['a', 'b'].map((name) => ({ id: name, name, displayName: name }));
  const props = { sessionId: 'new', groups, loading: false, loadError: false,
    ensureSession: async () => 'real', onCancel: () => {} };
  await act(async () => root.render(React.createElement(Picker, props)));
  await act(async () => {
    store.getState().toggle('new', 'b');
    store.getState().toggle('new', 'a');
  });
  const badge = () => document.querySelector('[data-testid="chat-panel-expert-batch-owner"]');
  assert.equal(badge().closest('button').getAttribute('data-variant'), 'b');
  assert.equal(badge().textContent, '主专家团');
  await act(async () => store.getState().toggle('new', 'b'));
  assert.equal(badge().closest('button').getAttribute('data-variant'), 'a');
  await act(async () => {
    selector.setState({ runtimes: { existing: { loaded: true, teams: [
      { team_id: 'owner', agent_group_name: 'b', is_owner: true },
    ] } } });
    store.getState().toggle('existing', 'a');
    root.render(React.createElement(Picker, { ...props, sessionId: 'existing' }));
  });
  const owner = badge().closest('button');
  assert.equal(owner.getAttribute('data-variant'), 'b');
  assert.equal(owner.disabled, true);
  assert.equal(owner.getAttribute('aria-checked'), 'true');
  await act(async () => root.unmount());
});

test('draft confirmation moves progress to the real session, deduplicates clicks, and keeps only failures selected', async () => {
  const calls = [];
  let finish;
  globalThis.batchRequest = (...args) => { calls.push(args); return new Promise((resolve) => { finish = resolve; }); };
  store.getState().toggle('new', 'a');
  store.getState().toggle('new', 'b');
  const submission = store.getState().submit('new', async () => 'real');
  await Promise.resolve();
  assert.equal(store.getState().runtimes.real.submitting, true);
  await store.getState().submit('real', async () => 'real');
  assert.equal(calls.length, 1);
  assert.deepEqual(calls[0][1].agent_group_names, ['a', 'b']);
  assert.equal(calls[0][1].session_id, 'real');
  finish({ results: [
    { agent_group_name: 'a', status: 'created', team_id: 'team-a' },
    { agent_group_name: 'b', status: 'failed', error: 'disabled' },
  ] });
  await submission;
  assert.deepEqual(store.getState().runtimes.real.selected, ['b']);
  assert.deepEqual(store.getState().runtimes.new.selected, []);
  assert.deepEqual(globalThis.batchRefreshes, ['real']);
});

test('a timed-out request keeps the selection for a safe server-side deduplicated retry', async () => {
  store.getState().toggle('s', 'a');
  globalThis.batchRequest = async () => { throw new Error('timeout'); };
  await store.getState().submit('s', async () => 's');
  assert.deepEqual(store.getState().runtimes.s.selected, ['a']);
  assert.equal(store.getState().runtimes.s.submitting, false);
  let sent;
  globalThis.batchRequest = async (_method, params) => {
    sent = params;
    return { results: [{ agent_group_name: 'a', status: 'existing', team_id: 'original' }] };
  };
  await store.getState().submit('s', async () => 's');
  assert.deepEqual(sent.agent_group_names, ['a']);
  assert.deepEqual(store.getState().runtimes.s.selected, []);
});

test('picker supports multiple checkboxes and cancel has no creation side effects', async () => {
  let ensured = 0;
  let sent = 0;
  let cancelled = 0;
  globalThis.batchRequest = async () => { sent++; return { results: [] }; };
  const root = createRoot(document.getElementById('root'));
  const groups = ['a', 'b'].map((name) => ({ id: name, name, displayName: name }));
  await act(async () => root.render(React.createElement(Picker, {
    sessionId: 'new', groups, loading: false, loadError: false,
    ensureSession: async () => { ensured++; return 'real'; }, onCancel: () => { cancelled++; },
  })));
  const items = [...document.querySelectorAll('[role="menuitemcheckbox"]')];
  assert.equal(document.querySelector('[data-testid="chat-panel-expert-batch-picker"]').firstElementChild, items[0]);
  await act(async () => { items[0].click(); items[1].click(); });
  assert.deepEqual(items.map((item) => item.getAttribute('aria-checked')), ['true', 'true']);
  assert.equal(document.querySelector('[data-testid="chat-panel-expert-batch-confirm"]').textContent, '确定（2）');
  assert.equal(sent, 0);
  await act(async () => document.querySelector('[data-testid="chat-panel-expert-batch-cancel"]').click());
  assert.equal(cancelled, 1);
  assert.equal(ensured, 0);
  assert.equal(sent, 0);
  assert.deepEqual(store.getState().runtimes.new.selected, []);
  await act(async () => root.unmount());
});

test('real locale resources render readable loading and confirmation labels in both languages', async () => {
  const root = createRoot(document.getElementById('root'));
  for (const [language, confirm, loading] of [
    ['zh', '确定（1）', '正在拉起专家团…'],
    ['en', 'Confirm (1)', 'Starting expert teams…'],
  ]) {
    await act(async () => {
      await i18next.changeLanguage(language);
      store.setState({ runtimes: { s: { selected: ['a'], submitting: false, results: [], error: null } } });
      root.render(React.createElement(Picker, {
        sessionId: 's', groups: [{ id: 'a', name: 'a', displayName: 'A' }],
        loading: false, loadError: false, ensureSession: async () => 's', onCancel: () => {},
      }));
    });
    assert.equal(document.querySelector('[data-testid="chat-panel-expert-batch-confirm"]').textContent, confirm);
    await act(async () => store.setState({ runtimes: {
      s: { selected: ['a'], submitting: true, results: [], error: null },
    } }));
    assert.equal(document.querySelector('[data-testid="chat-panel-expert-batch-confirm"]').textContent, loading);
  }
  await act(async () => root.unmount());
  await i18next.changeLanguage('zh');
});

test('picker uses catalog icons and falls back on missing or broken images without losing selection', async () => {
  const root = createRoot(document.getElementById('root'));
  const groups = [
    { id: 'business', name: 'business', displayName: '商业策划', avatarUrl: '/catalog/business.png' },
    { id: 'tech', name: 'tech', displayName: '技术洞察', avatarUrl: null },
  ];
  const props = {
    sessionId: 'new', groups, loading: false, loadError: false,
    ensureSession: async () => 'real', onCancel: () => {},
  };
  await act(async () => root.render(React.createElement(Picker, props)));
  const business = document.querySelector('[data-testid="chat-panel-expert-batch-item"][data-variant="business"]');
  assert.equal(business.querySelector('img').getAttribute('src'), '/catalog/business.png');
  assert.equal(document.querySelector('[data-testid="chat-panel-expert-batch-avatar"][data-variant="tech"]').textContent, '技');
  await act(async () => business.click());
  await act(async () => business.querySelector('img').dispatchEvent(new dom.window.Event('error')));
  assert.equal(business.querySelector('img'), null);
  assert.equal(business.querySelector('[data-testid="chat-panel-expert-batch-avatar"]').textContent, '商');
  assert.equal(business.getAttribute('aria-checked'), 'true');
  // Updating a catalog icon can retry the new URL instead of permanently
  // suppressing all future images for this group.
  await act(async () => root.render(React.createElement(Picker, {
    ...props, groups: [{ ...groups[0], avatarUrl: '/catalog/business-new.png' }, groups[1]],
  })));
  assert.equal(business.querySelector('img').getAttribute('src'), '/catalog/business-new.png');
  await act(async () => root.unmount());
});

test('only failures create conversation bubbles, including failed session creation', async () => {
  store.getState().toggle('new', 'a');
  await store.getState().submit('new', async () => null);
  assert.equal(globalThis.batchMessages.length, 1);
  assert.equal(globalThis.batchMessages[0].id, 'new');
  assert.equal(globalThis.batchMessages[0].message.role, 'assistant');
  globalThis.batchMessages = [];
  globalThis.batchRequest = async () => ({ results: [
    {agent_group_name: 'a', status: 'created'},
    {agent_group_name: 'b', status: 'failed', error: 'unavailable'},
  ] });
  await store.getState().submit('new', async () => 'real');
  assert.equal(globalThis.batchMessages.length, 1);
  assert.equal(globalThis.batchMessages[0].id, 'real');
  assert.match(globalThis.batchMessages[0].message.content, /b: unavailable/);
  assert.deepEqual(store.getState().runtimes.real.selected, ['b']);
  globalThis.batchRequest = async () => ({ results: [{agent_group_name: 'b', status: 'existing'}] });
  await store.getState().submit('real', async () => 'real');
  assert.equal(globalThis.batchMessages.length, 1);
});

test('confirmation closes the picker and results stay out of the selection panel', async () => {
  const root = createRoot(document.getElementById('root'));
  let closed = 0;
  globalThis.batchRequest = async () => ({ results: [
    {agent_group_name: 'a', status: 'failed', error: 'unavailable'},
  ] });
  await act(async () => root.render(React.createElement(Picker, {
    sessionId: 'new', groups: [{id: 'a', name: 'a', displayName: 'A'}],
    loading: false, loadError: false, ensureSession: async () => 'real',
    onCancel: () => { closed++; },
  })));
  await act(async () => document.querySelector('[role="menuitemcheckbox"]').click());
  await act(async () => document.querySelector('[data-testid="chat-panel-expert-batch-confirm"]').click());
  assert.equal(closed, 1);
  assert.equal(globalThis.batchMessages.length, 1);
  assert.equal(document.querySelector('[data-testid="chat-panel-expert-batch-results"]'), null);
  assert.equal(document.querySelector('[data-testid="chat-panel-expert-batch-error"]'), null);
  await act(async () => root.unmount());
});
