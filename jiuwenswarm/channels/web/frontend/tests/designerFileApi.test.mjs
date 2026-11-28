import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import { pipeline } from 'node:stream/promises';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import vm from 'node:vm';
import ts from 'typescript';

const frontend = fileURLToPath(new URL('..', import.meta.url));
const repository = path.resolve(frontend, '../../../..');
const venvPython = path.join(repository, '.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
const python = process.env.PYTHON || (fs.existsSync(venvPython) ? venvPython : process.platform === 'win32' ? 'python' : 'python3');
const source = fs.readFileSync(path.join(frontend, 'vite.config.ts'), 'utf8');
const tree = ts.createSourceFile('vite.config.ts', source, ts.ScriptTarget.Latest, true);
// Keep the real path resolver, allowlist and handlers together. Only Vite's
// imports and exported configuration are omitted; no file policy is mocked.
const script = tree.statements
  .filter(node => !ts.isImportDeclaration(node) && !ts.isExportAssignment(node))
  .map(node => node.getText(tree)).join('\n');
const compiled = ts.transpileModule(script, {
  compilerOptions: { target: ts.ScriptTarget.ES2020, module: ts.ModuleKind.CommonJS },
}).outputText;

function designPaths(root) {
  // Ask the backend producer for current paths, independently of Vite's policy.
  // A backend layout change should pass when the file API still serves it.
  const output = execFileSync(python, ['-c', `
import json
from jiuwenswarm.common.utils import get_agent_root_dir
from jiuwenswarm.server.runtime.designer.handlers.common import canvas_output_dir
directory = canvas_output_dir({'metadata': {'session_id': 'design_saved'}})
print('DESIGN_PATHS=' + json.dumps({'directory': str(directory), 'agentRoot': str(get_agent_root_dir())}))
`], {
    cwd: repository,
    env: { ...process.env, JIUWENSWARM_DATA_DIR: root, JIUWENSWARM_EDITION: 'personal' },
    encoding: 'utf8',
    timeout: 60000,
  });
  const result = output.split(/\r?\n/).find(line => line.startsWith('DESIGN_PATHS='));
  assert.ok(result, 'backend must return Design output paths');
  return JSON.parse(result.slice('DESIGN_PATHS='.length));
}

async function harness(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'designer-file-api-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const paths = designPaths(root);
  const handlers = [];
  const context = vm.createContext({
    fs, os, path, Buffer, URL, TextDecoder, pipeline, console,
    process: { ...process, env: { ...process.env, JIUWENSWARM_ROOT: root } },
    __dirname: frontend,
  });
  vm.runInContext(compiled, context);
  context.devFileContentApi().configureServer({
    config: { logger: { error: message => assert.fail(message) } },
    middlewares: { use: (...args) => { if (args.length === 2) handlers.push(args); } },
  });
  const server = http.createServer((req, res) => {
    const route = handlers.find(([prefix]) => new URL(req.url, 'http://localhost').pathname === prefix);
    if (route) route[1](req, res);
    else { res.statusCode = 404; res.end(); }
  });
  t.after(async () => {
    server.closeAllConnections();
    await new Promise(resolve => server.close(resolve));
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;
  return {
    root,
    ...paths,
    read: (endpoint, file, options) => fetch(`${base}/file-api/${endpoint}?path=${encodeURIComponent(file)}`, options),
  };
}

for (const style of ['absolute', 'relative', 'backslash']) {
  test(`dev file API reads saved Design media and Markdown using ${style} paths`, async t => {
    const h = await harness(t);
    const directory = h.directory;
    const relative = path.relative(h.root, directory).split(path.sep).join('/');
    fs.mkdirSync(directory, { recursive: true });
    for (const [name, endpoint, content] of [
      ['brief.md', 'file-content', '# Saved brief'],
      ['frame.png', 'raw-file', 'saved image bytes'],
      ['clip.mp4', 'raw-file', 'saved video bytes'],
    ]) {
      fs.writeFileSync(path.join(directory, name), content);
      const rel = `${relative}/${name}`;
      const file = style === 'absolute' ? path.join(directory, name)
        : style === 'backslash' ? rel.replaceAll('/', '\\') : rel;
      const response = await h.read(endpoint, file);
      assert.equal(response.status, 200, `${endpoint}: ${file}`);
      assert.equal(await response.text(), content);
    }
    const video = `${relative}/clip.mp4`;
    const range = await h.read('raw-file', video, { headers: { Range: 'bytes=0-4' } });
    assert.equal(range.status, 206);
    assert.equal(await range.text(), 'saved');
  });
}

test('allowing Design outputs keeps outside files and sibling-prefix traversal forbidden', async t => {
  const h = await harness(t);
  const agent = path.relative(h.root, h.agentRoot).split(path.sep).join('/');
  for (const file of ['private.txt', `${agent}-neighbor/private.txt`, `${agent}/../private.txt`]) {
    const target = path.join(h.root, file);
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(target, 'outside allowed roots');
    for (const endpoint of ['raw-file', 'file-content']) {
      const response = await h.read(endpoint, file);
      assert.equal(response.status, 403);
      assert.equal((await response.json()).error, 'forbidden_path');
    }
  }
  assert.equal((await h.read('raw-file', path.join(h.directory, 'missing.png'))).status, 404);
});
