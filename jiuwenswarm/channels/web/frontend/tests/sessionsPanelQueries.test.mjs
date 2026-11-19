import assert from 'node:assert/strict';
import test from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: false,
  cacheDir: 'node_modules/.cache/session-panel-queries/vite',
  esbuild: { jsx: 'automatic' },
  server: { middlewareMode: true, hmr: false, watch: null },
  define: { 'import.meta.env.DEV': false },
  plugins: [{
    name: 'session-panel-preview-fixture',
    enforce: 'pre',
    resolveId(source) {
      if (source.replace(/\\/g, '/').replace(/\.tsx$/, '').endsWith('/AgentPanel/FileViewer')) return '\0session-preview';
    },
    load(id) {
      if (id === '\0session-preview') return 'export function FileViewer() { return null; }';
    },
  }],
});
let modules;
try {
  modules = await Promise.all([
    vite.ssrLoadModule('/src/components/SessionsPanel/index.tsx'),
    vite.ssrLoadModule('/src/services/webClient.ts'),
    vite.ssrLoadModule('/src/stores/workspaceStore.ts'),
  ]);
} finally {
  await vite.close();
}
const [{ SessionsPanel }, { webClient }, { useWorkspaceStore }] = modules;
const initialWorkspace = useWorkspaceStore.getState();

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

const session = (id) => ({ session_id: id, title: id, channel_id: 'web', mode: 'agent.work.normal' });
const fileResponse = (id) => ({ ok: true, json: async () => ({ files: [{
  name: `${id}.md`, path: `agent/sessions/${id}/${id}.md`, isMarkdown: true, isDirectory: false,
}] }) });

function mount(t, request, fetchFiles = async (url) => {
  const parsed = new URL(url, 'http://localhost');
  return parsed.pathname === '/file-api/list-files'
    ? fileResponse(parsed.searchParams.get('dir').split('/').at(-1)) : { ok: false };
}) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost:5173' });
  const descriptors = new Map();
  for (const [name, value] of Object.entries({
    window: dom.window, document: dom.window.document, navigator: dom.window.navigator,
    HTMLElement: dom.window.HTMLElement, Node: dom.window.Node, localStorage: dom.window.localStorage,
    IS_REACT_ACT_ENVIRONMENT: true,
  })) {
    descriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  useWorkspaceStore.setState({ ...initialWorkspace, workMode: 'code' }, true);
  t.mock.method(webClient, 'request', (method, params) => {
    if (method === 'project.list') return Promise.resolve({ projects: [] });
    if (method === 'project.pinned_sessions') return Promise.resolve({ sessions: [] });
    assert.equal(method, 'session.list');
    return request(params);
  });
  t.mock.method(globalThis, 'fetch', fetchFiles);
  t.mock.method(console, 'error', () => {});
  const root = createRoot(document.getElementById('root'));
  const restored = [];
  const props = { currentSessionId: '', isConnected: true, isProcessing: true,
    onRestoreSession: (id) => restored.push(id) };
  const render = (patch = {}) => act(async () => root.render(createElement(SessionsPanel, { ...props, ...patch })));
  t.after(async () => {
    await act(async () => root.unmount());
    for (const [name, descriptor] of descriptors) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
    dom.window.close();
  });
  return { root, render, restored,
    find: (id) => document.querySelector(`[data-testid="${id}"]`),
    rows: () => [...document.querySelectorAll('[data-testid="sessions-panel-session-row"]')]
      .map(row => row.getAttribute('data-variant')),
  };
}

for (const outcome of ['success', 'failure']) {
  test(`a late session-list ${outcome} cannot replace current rows, files or selection`, async (t) => {
    const older = deferred();
    let calls = 0;
    const panel = mount(t, () => (++calls === 1 ? older.promise
      : Promise.resolve({ sessions: [session('sess_new')] })));
    await panel.render();
    await panel.render({ isProcessing: false });
    await act(async () => {
      if (outcome === 'success') older.resolve({ sessions: [session('sess_old')] });
      else older.reject(new Error('older list failed'));
    });
    assert.deepEqual(panel.rows(), ['sess_new']);
    assert.equal(panel.find('sessions-panel-error'), null);
    assert.match(panel.find('sessions-panel-file-item').textContent, /sess_new\.md/);
    await act(async () => panel.find('sessions-panel-restore-button').click());
    assert.deepEqual(panel.restored, ['sess_new']);
  });

  test(`an older list ${outcome} cannot clear a newer loading state`, async (t) => {
    const older = deferred();
    const newer = deferred();
    let calls = 0;
    const panel = mount(t, () => (++calls === 1 ? older.promise : newer.promise));
    await panel.render();
    await panel.render({ isProcessing: false });
    await act(async () => {
      if (outcome === 'success') older.resolve({ sessions: [] });
      else older.reject(new Error('older list failed'));
    });
    const loading = panel.find('sessions-panel-refresh-button').disabled;
    await act(async () => newer.resolve({ sessions: [session('sess_new')] }));
    assert.equal(loading, true);
    assert.deepEqual(panel.rows(), ['sess_new']);
    assert.equal(panel.find('sessions-panel-refresh-button').disabled, false);
  });
}

test('a slow file list cannot replace the newly selected session files', async (t) => {
  const older = deferred();
  const panel = mount(t, async () => ({ sessions: [session('sess_a'), session('sess_b')] }), async (url) => {
    const parsed = new URL(url, 'http://localhost');
    if (parsed.pathname !== '/file-api/list-files') return { ok: false };
    return parsed.searchParams.get('dir').endsWith('/sess_a') ? older.promise : fileResponse('sess_b');
  });
  await panel.render({ isProcessing: false });
  await act(async () => document.querySelectorAll('[data-testid="sessions-panel-session-item"]')[1].click());
  await act(async () => older.resolve(fileResponse('sess_a')));
  assert.match(panel.find('sessions-panel-file-item').textContent, /sess_b\.md/);
  await act(async () => panel.find('sessions-panel-restore-button').click());
  assert.deepEqual(panel.restored, ['sess_b']);
});

test('a stale file error cannot clear the current file loading state', async (t) => {
  const older = deferred();
  const newer = deferred();
  const panel = mount(t, async () => ({ sessions: [session('sess_a'), session('sess_b')] }), async (url) => {
    const parsed = new URL(url, 'http://localhost');
    if (parsed.pathname !== '/file-api/list-files') return { ok: false };
    return parsed.searchParams.get('dir').endsWith('/sess_a') ? older.promise : newer.promise;
  });
  await panel.render({ isProcessing: false });
  await act(async () => document.querySelectorAll('[data-testid="sessions-panel-session-item"]')[1].click());
  await act(async () => older.reject(new Error('older file query failed')));
  const loading = panel.find('sessions-panel-files-empty')?.getAttribute('data-variant');
  const failed = panel.find('sessions-panel-files-error') !== null;
  await act(async () => newer.resolve(fileResponse('sess_b')));
  assert.equal(loading, 'loading');
  assert.equal(failed, false);
  assert.match(panel.find('sessions-panel-file-item').textContent, /sess_b\.md/);
});

test('work-mode round trips invalidate pending lists and reload the current mode', async (t) => {
  const older = deferred();
  const modes = [];
  const panel = mount(t, (params) => {
    modes.push(params.work_mode);
    return modes.length === 1 ? older.promise : Promise.resolve({ sessions: [session(`sess_${params.work_mode}`)] });
  });
  await panel.render({ isProcessing: false });
  await act(async () => useWorkspaceStore.getState().setWorkMode('work'));
  await act(async () => useWorkspaceStore.getState().setWorkMode('code'));
  await act(async () => older.resolve({ sessions: [session('sess_old_code')] }));
  assert.deepEqual(modes, ['code', 'work', 'code']);
  assert.deepEqual(panel.rows(), ['sess_code']);
});

test('unmounting with a pending list does not start file queries afterward', async (t) => {
  const pending = deferred();
  const fileQueries = [];
  const panel = mount(t, () => pending.promise, async (url) => { fileQueries.push(url); return { ok: false }; });
  await panel.render();
  await act(async () => panel.root.unmount());
  await act(async () => pending.resolve({ sessions: [session('sess_late')] }));
  assert.deepEqual(fileQueries, []);
});

test('a current list failure still displays an error and ends loading', async (t) => {
  const panel = mount(t, async () => { throw new Error('current list failure'); });
  await panel.render({ isProcessing: false });
  assert.notEqual(panel.find('sessions-panel-error'), null);
  assert.equal(panel.find('sessions-panel-refresh-button').disabled, false);
  assert.deepEqual(panel.rows(), []);
});

test('a late todo probe cannot replace files for a newly selected session', async (t) => {
  const todo = deferred();
  const panel = mount(t, async () => ({ sessions: [session('sess_a'), session('sess_b')] }), async (url) => {
    const parsed = new URL(url, 'http://localhost');
    if (parsed.pathname === '/file-api/list-files') return fileResponse(parsed.searchParams.get('dir').split('/').at(-1));
    return parsed.searchParams.get('path').includes('/sess_a/') ? todo.promise : { ok: false };
  });
  await panel.render({ isProcessing: false });
  await act(async () => document.querySelectorAll('[data-testid="sessions-panel-session-item"]')[1].click());
  await act(async () => todo.resolve({ ok: true }));
  assert.match(panel.find('sessions-panel-file-item').textContent, /sess_b\.md/);
  assert.equal(document.querySelectorAll('[data-testid="sessions-panel-file-item"]').length, 1);
});

test('unmounting with a pending file list stops child traversal and todo probes', async (t) => {
  const files = deferred();
  const queries = [];
  const panel = mount(t, async () => ({ sessions: [session('sess_a')] }), async (url) => {
    queries.push(url);
    return queries.length === 1 ? files.promise : { ok: true, json: async () => ({ files: [] }) };
  });
  await panel.render({ isProcessing: false });
  await act(async () => panel.root.unmount());
  await act(async () => files.resolve({ ok: true, json: async () => ({ files: [{
    name: 'nested', path: 'agent/sessions/sess_a/nested', isMarkdown: false, isDirectory: true,
  }] }) }));
  assert.equal(queries.length, 1);
});
