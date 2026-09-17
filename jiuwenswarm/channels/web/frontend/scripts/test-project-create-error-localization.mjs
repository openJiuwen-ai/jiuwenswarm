import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import { transform } from 'esbuild';

const sourcePath = new URL('../src/multi-session/sidebar/projectCreateErrors.ts', import.meta.url);
const source = await readFile(sourcePath, 'utf8');
const compiled = await transform(source, {
  loader: 'ts',
  format: 'esm',
});
const moduleUrl = `data:text/javascript;base64,${Buffer.from(compiled.code).toString('base64')}`;
const { projectCreateErrorKey } = await import(moduleUrl);

assert.equal(
  projectCreateErrorKey(new Error('project_dir already exists')),
  'multiSession.project.errors.pathExists',
);
assert.equal(
  projectCreateErrorKey(new Error('Error: project_dir already exists')),
  'multiSession.project.errors.pathExists',
);
assert.equal(projectCreateErrorKey(new Error('permission denied')), null);

const zh = JSON.parse(await readFile(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
const en = JSON.parse(await readFile(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8'));

assert.equal(typeof zh.multiSession.project.errors.pathExists, 'string');
assert.equal(typeof en.multiSession.project.errors.pathExists, 'string');
assert.notEqual(zh.multiSession.project.errors.pathExists, 'project_dir already exists');
assert.notEqual(en.multiSession.project.errors.pathExists, 'project_dir already exists');

console.log('project create error localization ok');
