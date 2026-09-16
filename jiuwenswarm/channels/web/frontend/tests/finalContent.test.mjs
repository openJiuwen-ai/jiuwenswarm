import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeFinalContent } from '../node_modules/.cache/final-content/finalContent.mjs';

test('回答本身是含 output 字段的 JSON 对象时，final 正文保持完整', () => {
  const body = '{"output": "唯一结果", "usage": {"tokens": 42}}';
  assert.equal(
    normalizeFinalContent({ content: body }),
    body
  );
});

test('回答包含 JSON 代码块（含 output 字段）时，final 正文保持完整', () => {
  const body = [
    '调用示例如下：',
    '',
    '```json',
    '{"output": "内部值", "result_type": "answer"}',
    '```',
    '',
    '以上字段仅作示例说明。',
  ].join('\n');
  assert.equal(
    normalizeFinalContent({ content: body }),
    body
  );
});

test('回答包含 delta.content 结构的 JSON 示例时，final 正文保持完整', () => {
  const body = '{"delta": {"content": "增量片段"}, "rid": "req_1"}';
  assert.equal(
    normalizeFinalContent({ content: body }),
    body
  );
});

test('回答包含 Python 字典文本（单引号 output）时，final 正文保持完整', () => {
  const body = "{'output': '字典值', 'result_type': 'answer'}";
  assert.equal(
    normalizeFinalContent({ content: body }),
    body
  );
});

test('普通文本正文原样保留（含前后说明文字的完整回答）', () => {
  const body = '第一段说明。\n\n第二段：详见 {\\n  "output": "x"\\n} 字段。';
  assert.equal(
    normalizeFinalContent({ content: body }),
    body
  );
});

test('展示层归一保留：字面 \\n 还原为真换行、去掉开头空行', () => {
  const table = '| a | b |\\n|---|---|\\n| 1 | 2 |';
  assert.equal(
    normalizeFinalContent({ content: `\\n\\n${table}` }),
    '| a | b |\n|---|---|\n| 1 | 2 |'
  );
  assert.equal(
    normalizeFinalContent({ content: '\n\n开头空行会被去掉' }),
    '开头空行会被去掉'
  );
});

test('非字符串 content 返回空串', () => {
  assert.equal(normalizeFinalContent({ content: null }), '');
  assert.equal(normalizeFinalContent({ content: { output: 'x' } }), '');
  assert.equal(normalizeFinalContent({}), '');
});
