import test from 'node:test';
import assert from 'node:assert/strict';

import {
  clearMemberCompressionEntriesForSession,
  contextCompressionInterruptedText,
  memberCompressionKey,
  nextMemberCompressionSummary,
} from '../node_modules/.cache/context-compression-cancel/contextCompression.mjs';

/** i18n 桩：原样回显 key，断言只关心命中的词条。 */
const t = (key) => `i18n:${key}`;

test('cancelled and paused map to interrupted copy', () => {
  assert.equal(
    contextCompressionInterruptedText(t, 'cancelled'),
    'i18n:chat.contextCompressionCancelled',
  );
  assert.equal(
    contextCompressionInterruptedText(t, 'paused'),
    'i18n:chat.contextCompressionPaused',
  );
});

test('other statuses fall back to null so callers keep the backend summary', () => {
  for (const status of ['completed', 'failed', 'running', 'unchanged', 'skipped', '', undefined]) {
    assert.equal(contextCompressionInterruptedText(t, status), null, String(status));
  }
});

test('member compression key embeds the session id behind a NUL separator', () => {
  const key = memberCompressionKey('session-a', 'writer');
  assert.equal(key, 'session-a\u0000writer');
  // 前缀按整段会话 id 匹配，"s1" 不得误命中 "s10" 的键（NUL 收口）。
  assert.ok(!key.startsWith('session-\u0000'));
  assert.ok(memberCompressionKey('s10', 'm').startsWith('s10\u0000'));
  assert.ok(!memberCompressionKey('s10', 'm').startsWith('s1\u0000'));
});

test('clearing by session prefix removes all of the session entries and keeps other sessions', () => {
  const entries = new Map([
    [memberCompressionKey('s1', 'a'), { count: 1, summaries: ['x'] }],
    [memberCompressionKey('s1', 'b'), { count: 2, summaries: ['x', 'y'] }],
    [memberCompressionKey('s10', 'a'), { count: 1, summaries: ['x'] }],
    [memberCompressionKey('s2', 'c'), { count: 1, summaries: ['x'] }],
  ]);
  clearMemberCompressionEntriesForSession(entries, 's1');
  assert.deepEqual([...entries.keys()].sort(), ['s10\u0000a', 's2\u0000c']);
  // 清理不存在的会话是 no-op。
  clearMemberCompressionEntriesForSession(entries, 'missing');
  assert.equal(entries.size, 2);
});

test('X-closed member entries are cleared by prefix even though the store no longer knows them', () => {
  // CR-001 残余路径回归：turn 内成员压缩条被 X 关闭后 store 记录已删，
  // turn 收尾只能按 ref 键前缀清，否则该成员下一 turn 计数被抬高。
  const entries = new Map([[memberCompressionKey('s1', 'ghost'), { count: 3, summaries: ['a', 'b', 'c'] }]]);
  clearMemberCompressionEntriesForSession(entries, 's1');
  assert.equal(entries.size, 0);
});

test('next summary accumulates count and detail list together from the turn-scoped base', () => {
  const first = nextMemberCompressionSummary(undefined, '压缩了 10 轮');
  assert.deepEqual(first, { count: 1, summaries: ['压缩了 10 轮'] });

  const second = nextMemberCompressionSummary(first, '压缩了 20 轮');
  assert.deepEqual(second, {
    count: 2,
    summaries: ['压缩了 10 轮', '压缩了 20 轮'],
  });
});

test('next summary from a cleared base does not inherit the retained display summary', () => {
  // CR-002 残余路径回归：turn 收尾清掉基数后，completed 只从 ref 起算，
  // 上一 turn 留在展示位的 summary（count=2、两条详情）不再被续接。
  const retainedDisplay = { count: 2, summaries: ['旧详情 1', '旧详情 2'] };
  const fresh = nextMemberCompressionSummary(undefined, '本 turn 详情');
  assert.deepEqual(fresh, { count: 1, summaries: ['本 turn 详情'] });
  assert.notEqual(fresh.count, retainedDisplay.count);
  assert.ok(!fresh.summaries.some((item) => item.startsWith('旧详情')));
});
