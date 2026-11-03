import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';

const mocks = {
  './rsiApi': `
    export const rsiArtifactFilesList = (...args) => globalThis.rsiPreviewProbe.list(...args);
    export const rsiArtifactFilesGet = (...args) => globalThis.rsiPreviewProbe.read(...args);
  `,
  '../rsiStore': 'export const useRsiStore = selector => selector(globalThis.rsiPreviewState);',
  'react-i18next': 'const t = key => key; export const useTranslation = () => ({ t });',
  '../../../components/ArtifactsPanel/FilePreview': `
    import React from 'react';
    export const FilePreview = ({ artifact }) => React.createElement('pre', null, artifact.name + ' ' + artifact.mimeType);
  `,
  './RsiLatexPreview': 'export const RsiLatexPreview = () => null;',
};
await build({
  entryPoints: [
    'src/features/rsi/rsiArtifactFiles.ts',
    'src/features/rsi/components/RsiArtifactDetailDialog.tsx',
    'src/features/rsi/components/RsiSelectedInfo.tsx',
    'src/features/rsi/components/RsiResultSummary.tsx',
  ],
  outdir: 'node_modules/.cache/rsi-harness-preview', outbase: 'src/features/rsi',
  outExtension: { '.js': '.mjs' }, bundle: true, platform: 'node', format: 'esm',
  packages: 'external', jsx: 'automatic', loader: { '.svg': 'dataurl' },
  plugins: [{ name: 'preview-boundaries', setup(builder) {
    builder.onResolve({ filter: /^react(?:\/.*)?$/ }, args => ({ path: args.path, external: true }));
    builder.onResolve({ filter: /.*/ }, args => args.path in mocks ? { path: args.path, namespace: 'mock' } : undefined);
    builder.onLoad({ filter: /.*/, namespace: 'mock' }, args => ({ contents: mocks[args.path] }));
  } }],
});
const files = await import('../node_modules/.cache/rsi-harness-preview/rsiArtifactFiles.mjs');
const { RsiArtifactDetailDialog } = await import('../node_modules/.cache/rsi-harness-preview/components/RsiArtifactDetailDialog.mjs');
const { RsiSelectedInfo } = await import('../node_modules/.cache/rsi-harness-preview/components/RsiSelectedInfo.mjs');
const { RsiResultSummary } = await import('../node_modules/.cache/rsi-harness-preview/components/RsiResultSummary.mjs');
const tick = () => new Promise(resolve => setImmediate(resolve));
const node = (id, snapshot, type = 'ADOPTED') => ({
  node_id: id, snapshot_artifact_id: snapshot, type, adopted: type === 'ADOPTED', iteration: 1,
  parent_id: type === 'ROOT' ? null : 'ROOT', score: .5, changes: [],
  extra: { artifact_path: '/run/mutable/candidate_harness_refs.yaml' },
});

test('Harness source uses its immutable snapshot and never falls back to refs or another version', () => {
  assert.deepEqual(files.resolveRsiArtifactSource(node('e1', 'Aepoch-001'), 'task', 'HARNESS'), {
    taskId: 'task', artifactId: 'Aepoch-001', initialFilePath: null,
  });
  assert.equal(files.resolveRsiArtifactSource(node('e2', null, 'REJECTED'), 'task', 'HARNESS'), null);
  assert.equal(files.resolveRsiArtifactSource(node('ROOT', 'AROOT', 'ROOT'), 'task', 'HARNESS').artifactId, 'AROOT');
  assert.equal(files.resolveRsiArtifactSource(node('paper', 'A1'), 'task', 'ARTIFACT').path,
    '/run/mutable/candidate_harness_refs.yaml');
});

test('known source files are previewable despite a generic platform MIME', () => {
  for (const name of ['harness_config.yaml', 'config.yml', 'helper.py', 'tool.ts', 'README.md', 'config.toml']) {
    const entry = { name, path: name, mimeType: 'application/octet-stream', isDirectory: false };
    assert.equal(files.isPreviewableEntry(entry), true, name);
    assert.match(files.artifactMimeType(entry), /^text\//);
  }
  assert.equal(files.isPreviewableEntry({ name: 'key.db', path: 'key.db', mimeType: 'application/octet-stream' }), false);
});

async function mounted(run) {
  const dom = new JSDOM('<div id="root"></div>');
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const calls = [];
  globalThis.rsiPreviewProbe = {
    async list(taskId, selector) {
      calls.push(['list', taskId, selector]);
      const id = typeof selector === 'object' ? selector.artifact_id : selector;
      const root = '/cache/' + id;
      return { root, initial_path: root + '/harness_config.yaml', files: [
        { name: 'harness_config.yaml', path: root + '/harness_config.yaml', type: 'application/octet-stream', size: 10, isDirectory: false },
        { name: 'README.md', path: root + '/README.md', type: 'text/markdown', size: 10, isDirectory: false },
      ] };
    },
    async read(taskId, path) {
      calls.push(['read', taskId, path]);
      return { path, content: 'name: preview', type: 'text/plain', encoding: 'text' };
    },
  };
  globalThis.rsiPreviewState = { detail: { task: {
    task: { task_id: 'task', name: 'Harness', scenario: 'HARNESS', status: 'COMPLETED', config: {} },
    tree: { nodes: [node('ROOT', 'AROOT', 'ROOT'), node('e1', 'Aepoch-001')] }, selectedNodeId: 'ROOT',
  } }, setSelectedNode() {} };
  const root = createRoot(document.getElementById('root'));
  try { await run(root, calls); }
  finally {
    await act(async () => root.unmount());
    dom.window.close();
    delete globalThis.window;
    delete globalThis.document;
    delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    delete globalThis.rsiPreviewProbe;
    delete globalThis.rsiPreviewState;
  }
}

test('baseline detail button loads the package tree and its configuration', () => mounted(async (root, calls) => {
  await act(async () => root.render(React.createElement(RsiSelectedInfo, { taskId: 'task' })));
  const button = document.querySelector('.rsi-selected-info__detail-btn');
  assert.ok(button, 'baseline snapshot can be inspected');
  await act(async () => { button.click(); await tick(); });
  assert.ok(document.querySelector('.rsi-artifact-dialog__file--selected')?.textContent.includes('harness_config.yaml'));
  assert.deepEqual(calls[0], ['list', 'task', { artifact_id: 'AROOT' }]);
  assert.ok(calls.some(call => call[0] === 'read' && call[2] === '/cache/AROOT/harness_config.yaml'));
}));

test('changing only the snapshot ID reloads the dialog and drops the previous selection', () => mounted(async (root, calls) => {
  const render = artifactId => root.render(React.createElement(RsiArtifactDetailDialog, {
    source: { taskId: 'task', artifactId, initialFilePath: null }, title: artifactId, onClose() {},
  }));
  await act(async () => { render('Aepoch-001'); await tick(); });
  await act(async () => { render('Aepoch-003'); await tick(); });
  assert.deepEqual(calls.filter(call => call[0] === 'list'), [
    ['list', 'task', { artifact_id: 'Aepoch-001' }], ['list', 'task', { artifact_id: 'Aepoch-003' }],
  ]);
  assert.ok(calls.some(call => call[0] === 'read' && call[2] === '/cache/Aepoch-003/harness_config.yaml'));
}));

test('a node without a package shows an explanation instead of an empty detail action', () => mounted(async root => {
  globalThis.rsiPreviewState.detail.task.tree.nodes.push(node('e2', null, 'REJECTED'));
  globalThis.rsiPreviewState.detail.task.selectedNodeId = 'e2';
  await act(async () => root.render(React.createElement(RsiSelectedInfo, { taskId: 'task' })));
  assert.equal(document.querySelector('.rsi-selected-info__detail-btn'), null);
  assert.match(document.body.textContent, /rsi.artifact.noHarnessSnapshot/);
}));

test('the best Harness link opens the same snapshot source without downloading it', () => mounted(async root => {
  const task = globalThis.rsiPreviewState.detail.task.task;
  const opened = [];
  await act(async () => root.render(React.createElement(RsiResultSummary, {
    task, report: null, usage: null, onOpenArtifact: (...args) => opened.push(args),
  })));
  await act(async () => document.querySelector('[data-testid="rsi-best-link"]').click());
  assert.equal(opened[0][0].artifactId, 'Aepoch-001');
}));

test('an explicit best node ID selects its own snapshot rather than the latest node', () => mounted(async root => {
  const task = globalThis.rsiPreviewState.detail.task.task;
  task.best_artifact = { artifact_id: 'e1', name: 'best' };
  globalThis.rsiPreviewState.detail.task.tree.nodes.push({ ...node('e3', 'Aepoch-003'), iteration: 3 });
  const opened = [];
  const render = () => root.render(React.createElement(RsiResultSummary, {
    task: { ...task }, report: null, usage: null, onOpenArtifact: (...args) => opened.push(args),
  }));
  await act(async () => render());
  await act(async () => document.querySelector('[data-testid="rsi-best-link"]').click());
  assert.equal(opened[0][0].artifactId, 'Aepoch-001');
  task.best_artifact = { artifact_id: 'missing-node', name: 'unavailable' };
  await act(async () => render());
  assert.equal(document.querySelector('[data-variant="link"]'), null);
}));

test('file read errors are visible and selecting another file can recover', () => mounted(async root => {
  globalThis.rsiPreviewProbe.read = async () => { throw Error('file unavailable'); };
  await act(async () => {
    root.render(React.createElement(RsiArtifactDetailDialog, {
      source: { taskId: 'task', artifactId: 'AROOT', initialFilePath: null }, title: 'baseline', onClose() {},
    }));
    await tick();
  });
  assert.equal(document.querySelector('[role="alert"]').textContent, 'file unavailable');
  globalThis.rsiPreviewProbe.read = async (_task, path) => ({ path, content: 'readme', type: 'text/markdown', encoding: 'text' });
  const readme = [...document.querySelectorAll('.rsi-artifact-dialog__file')].find(button => button.textContent === 'README.md');
  await act(async () => { readme.click(); await tick(); });
  assert.equal(document.querySelector('[role="alert"]'), null);
  assert.equal(document.querySelector('.rsi-artifact-dialog__filename').textContent, 'README.md');
}));

test('a delayed previous snapshot cannot overwrite the current package tree', () => mounted(async root => {
  let finishOld;
  globalThis.rsiPreviewProbe.list = async (_task, selector) => {
    if (selector.artifact_id === 'Aold') await new Promise(resolve => { finishOld = resolve; });
    const name = selector.artifact_id === 'Aold' ? 'first.yaml' : 'third.yaml';
    return { root: '/cache', initial_path: '/cache/' + name, files: [
      { name, path: '/cache/' + name, size: 1, isDirectory: false, type: 'text/plain' },
    ] };
  };
  const render = artifactId => root.render(React.createElement(RsiArtifactDetailDialog, {
    source: { taskId: 'task', artifactId, initialFilePath: null }, title: artifactId, onClose() {},
  }));
  await act(async () => { render('Aold'); await tick(); });
  await act(async () => { render('Anew'); await tick(); });
  await act(async () => { finishOld(); await tick(); });
  assert.equal(document.querySelector('.rsi-artifact-dialog__file--selected').textContent, 'third.yaml');
  assert.equal(document.querySelector('.rsi-artifact-dialog__filename').textContent, 'third.yaml');
}));
