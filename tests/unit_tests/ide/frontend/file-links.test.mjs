// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import vm from 'node:vm';

const html = readFileSync(new URL(
  '../../../../jiuwenswarm/channels/ide/packages/shared-webview/chat.html', import.meta.url,
), 'utf8');

function loadRenderer() {
  const sent = [];
  const context = vm.createContext({ send: (message) => sent.push(message) });
  vm.runInContext(
    html.slice(html.indexOf('function escapeHtml('), html.indexOf('function truncate('))
      + html.slice(html.indexOf('function renderMarkdown('), html.indexOf('// Init: restore theme')),
    context,
  );
  return { context, sent };
}

const samples = [
  ['ide-plugin-install/check_model_cfg.py:12', 'ide-plugin-install/check_model_cfg.py', '12'],
  ['E:/mwl/swarm串讲插件-智能审批/code/ide-plugin-install/check_model_cfg.py:12',
    'E:/mwl/swarm串讲插件-智能审批/code/ide-plugin-install/check_model_cfg.py', '12'],
  ['check_model_cfg.py:12', 'check_model_cfg.py', '12'],
  ['ide-plugin-install/check_model_cfg.py', 'ide-plugin-install/check_model_cfg.py', '0'],
  ['C:\\项目 文件\\check_model_cfg.py:12', 'C:\\项目 文件\\check_model_cfg.py', '12'],
  ['/tmp/project/check_model_cfg.py:12', '/tmp/project/check_model_cfg.py', '12'],
];

for (const [reference, path, line] of samples) {
  test(`backtick file reference preserves path and line: ${reference}`, () => {
    const { context } = loadRenderer();
    const result = context.renderMarkdown('`' + reference + '`');
    assert.match(result, /class="file-link"/);
    assert.ok(result.includes('data-path="' + path + '"'));
    assert.ok(result.includes('data-line="' + line + '"'));
    assert.ok(result.includes('<code>' + reference + '</code>'));
    assert.equal((result.match(/<a /g) || []).length, 1);
    assert.doesNotMatch(result, /<em>/);
  });
}

test('ordinary inline code keeps underscores and Markdown characters literal', () => {
  const { context } = loadRenderer();
  assert.equal(context.renderMarkdown('`check_model_cfg.py` and `a * b`'),
    '<code>check_model_cfg.py</code> and <code>a * b</code>');
});

test('bare file references retain underscores and have one link', () => {
  const { context } = loadRenderer();
  const result = context.renderMarkdown('See src/check_model_cfg.py:12.');
  assert.ok(result.includes('data-path="src/check_model_cfg.py" data-line="12"'));
  assert.equal((result.match(/<a /g) || []).length, 1);
  assert.doesNotMatch(result, /<em>/);
});

test('code fences remain literal and prose emphasis still works', () => {
  const { context } = loadRenderer();
  assert.equal(context.renderMarkdown('```python\n`src/check_model_cfg.py:12`\n```'),
    '<pre><code>`src/check_model_cfg.py:12`</code></pre>');
  assert.equal(context.renderMarkdown('**bold** *italic* _emphasis_'),
    '<strong>bold</strong> <em>italic</em> <em>emphasis</em>');
});

test('message HTML is escaped before rendering', () => {
  const { context } = loadRenderer();
  const result = context.renderMarkdown('<img src=x onerror="alert(1)"> `src/a&b.py:12`');
  assert.doesNotMatch(result, /<img/);
  assert.match(result, /&lt;img/);
  assert.match(result, /data-path="src\/a&amp;b.py"/);
});

test('URLs and non-file code expressions stay ordinary inline code', () => {
  const { context } = loadRenderer();
  assert.equal(context.renderMarkdown('`https://example.com/test.py:12` `print(foo.py)`'),
    '<code>https://example.com/test.py:12</code> <code>print(foo.py)</code>');
});

test('file links do not reprocess each other or adjacent inline code', () => {
  const { context } = loadRenderer();
  const result = context.renderMarkdown('`src/foo.py:12` `a_b_c` src/bar.py:3');
  assert.equal((result.match(/<a /g) || []).length, 2);
  assert.match(result, /<code>a_b_c<\/code>/);
  assert.doesNotMatch(result, /<em>/);
});

test('clicking a file link sends the existing IDE bridge payload', () => {
  const { context, sent } = loadRenderer();
  context.openFileLink({ getAttribute: (name) => ({
    'data-path': 'E:/项目/check_model_cfg.py', 'data-line': '12',
  })[name] });
  assert.equal(JSON.stringify(sent), JSON.stringify([
    { type: 'open_file', path: 'E:/项目/check_model_cfg.py', line: 12 },
  ]));
});

test('split streaming references render without replacing interleaved tool cards', () => {
  const { context } = loadRenderer();
  const children = [];
  const stream = {
    get lastElementChild() { return children.at(-1); },
    appendChild: (child) => children.push(child),
  };
  context.state = { pendingTurns: { turn: { streamEl: stream, textContent: '', firstTokenTime: 1 } } };
  context.promoteThinkingPill = () => {};
  context.scrollToBottom = () => {};
  context.document = { createElement: () => ({
    textContent: '', innerHTML: '', className: '',
    classList: { contains: (name) => name === 'chunk-text' },
  }) };
  vm.runInContext(html.slice(html.indexOf('function appendDelta('), html.indexOf('function appendThinking(')), context);
  context.appendDelta('turn', '`src/check_');
  context.appendDelta('turn', 'model_cfg.py:12`');
  assert.match(children[0].innerHTML, /data-path="src\/check_model_cfg.py" data-line="12"/);
  const toolCard = { classList: { contains: () => false } };
  children.push(toolCard);
  context.appendDelta('turn', '`other/file.py:2`');
  assert.equal(children.length, 3);
  assert.equal(children[1], toolCard);
  assert.match(children[2].innerHTML, /data-path="other\/file.py" data-line="2"/);
  assert.equal(context.state.pendingTurns.turn.textContent,
    '`src/check_model_cfg.py:12``other/file.py:2`');
});
