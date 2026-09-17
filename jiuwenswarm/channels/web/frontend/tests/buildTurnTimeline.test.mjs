import assert from 'node:assert/strict';
import test from 'node:test';

import { buildRenderItems } from '../node_modules/.cache/build-turn-timeline/buildTurnTimeline.js';

const U = 1_700_000_000_000; // 用户消息时刻
const S = 1_700_000_005_000; // reasoning 首帧
const A = 1_700_000_035_000; // reasoning 末帧（updatedAt）

function iso(ms) {
  return new Date(ms).toISOString();
}

function userMessage(ms, id = 'u1') {
  return {
    type: 'message',
    key: id,
    timestampMs: ms,
    sourceIndex: 0,
    message: { id, role: 'user', content: 'hi', timestamp: iso(ms) },
  };
}

function reasoningItem(segment, sourceIndex = 0) {
  return {
    type: 'reasoning',
    key: segment.id,
    timestampMs: segment.startedAt,
    sourceIndex,
    segment,
  };
}

function turnSummaryOf(items) {
  return items.find((item) => item.type === 'turnSummary');
}

function execution({ status, startedAt, updatedAt }) {
  return {
    toolCallId: `tc-${startedAt}`,
    toolCall: { id: `tc-${startedAt}`, name: 'bash', arguments: {} },
    status,
    startedAt: iso(startedAt),
    updatedAt: iso(updatedAt),
    timeoutAt: iso(startedAt + 60_000),
  };
}

test('异常结束（无 closedAt）：reasoning.updatedAt 兜底为耗时终点', () => {
  const items = [
    userMessage(U),
    reasoningItem({
      id: 'rsn1',
      text: 'thinking…',
      startedAt: S,
      closed: false,
      updatedAt: A,
    }),
  ];
  const out = buildRenderItems(items, false, false);
  const summary = turnSummaryOf(out);
  assert.ok(summary, 'should emit turnSummary');
  assert.equal(summary.workEndMs, A, 'workEndMs 落在末帧 updatedAt');
  assert.equal(summary.startMs, U);
});

test('老数据向后兼容：无 updatedAt 时用 closedAt', () => {
  const closedAt = 1_700_000_020_000;
  const items = [
    userMessage(U),
    reasoningItem({
      id: 'rsn1',
      text: 'thinking…',
      startedAt: S,
      closed: true,
      closedAt,
    }),
  ];
  const summary = turnSummaryOf(buildRenderItems(items, false, false));
  assert.equal(summary.workEndMs, closedAt, '缺失 updatedAt 时退回 closedAt');
});

test('哨兵值：updatedAt 为 0 或过小毫秒数被忽略', () => {
  const closedAt = 1_700_000_020_000;
  for (const bad of [0, 500, 1_000_000]) {
    const items = [
      userMessage(U),
      reasoningItem({
        id: `rsn-${bad}`,
        text: 'thinking…',
        startedAt: S,
        closed: true,
        updatedAt: bad,
        closedAt,
      }),
    ];
    const summary = turnSummaryOf(buildRenderItems(items, false, false));
    assert.equal(summary.workEndMs, closedAt, `updatedAt=${bad} 不应撑爆耗时`);
  }
});

test('回归：pending/timeout 工具的 updatedAt 不计入耗时终点（防巡检污染）', () => {
  const toolStart = 1_700_000_010_000;
  const hugePollution = 1_900_000_000_000; // 巡检写成 Date.now() 的假时间
  const items = [
    userMessage(U),
    {
      type: 'toolExecution',
      key: 'tc-1',
      timestampMs: toolStart,
      sourceIndex: 0,
      execution: execution({ status: 'pending', startedAt: toolStart, updatedAt: hugePollution }),
    },
  ];
  const summary = turnSummaryOf(buildRenderItems(items, false, false));
  assert.equal(summary.workEndMs, toolStart, 'pending 的 updatedAt 不得进入 work 终点');
});
