import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { defaultCommitMessage, gitPublishErrorMessage, remoteNames } from '../node_modules/.cache/git-publish-state/features/code-mode/gitPublishState.js';

// 从 zh 词典构造最小翻译函数：t('a.b.c') 沿嵌套对象取值，缺键回退键名。
const zh = JSON.parse(await readFile(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
const t = (key) => key.split('.').reduce((node, part) => (node && typeof node === 'object' ? node[part] : undefined) ?? key, zh);

test('generates a fallback commit message from the changed file count', () => {
  assert.equal(defaultCommitMessage(0), 'Update project files');
  assert.equal(defaultCommitMessage(1), 'Update 1 file');
  assert.equal(defaultCommitMessage(3), 'Update 3 files');
});

test('derives unique remote names and falls back to origin', () => {
  assert.deepEqual(remoteNames(['origin/main', 'upstream/main', 'origin/dev']), ['origin', 'upstream']);
  assert.deepEqual(remoteNames([]), ['origin']);
});

test('maps known backend errors and preserves useful unknown errors', () => {
  assert.match(gitPublishErrorMessage({ code: 'NOTHING_TO_COMMIT' }, 'fallback', t), /没有可提交/);
  assert.match(gitPublishErrorMessage({ code: 'BRANCH_ALREADY_EXISTS' }, 'fallback', t), /已存在/);
  assert.match(gitPublishErrorMessage({ code: 'BRANCH_INVALID' }, 'fallback', t), /不符合 Git 规范/);
  assert.equal(gitPublishErrorMessage(new Error('custom failure'), 'fallback', t), 'custom failure');
  assert.equal(gitPublishErrorMessage(null, 'fallback', t), 'fallback');
});
