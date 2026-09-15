import assert from 'node:assert/strict';
import test from 'node:test';

import { buildRenderItems } from '../node_modules/.cache/build-turn-timeline/buildTurnTimeline.js';

const U = 1_700_000_000_000; // 用户消息时刻
const S = 1_700_000_005_000; // reasoning 首帧
const A = 1_700_000_035_000; // reasoning 末帧（updatedAt）

test('全双工简短确认和后续发言在运行中及完成后均保持展开', () => {
  for (const isTeam of [false, true]) {
    for (const isProcessing of [false, true]) {
      const ack = assistantMessage(U + 1_000, U + 1_000, 'spoken-ack');
      ack.message.content = '好的，没问题，我现在就帮你生成这道题的代码。';
      ack.message.keepExpanded = true;
      const result = assistantMessage(U + 2_000, U + 2_000, 'result');
      result.message.presentation = 'tool_result';
      const receipt = assistantMessage(U + 3_000, U + 3_000, 'spoken-receipt');
      receipt.message.keepExpanded = true;
      const out = buildRenderItems([userMessage(U), ack, result, receipt], isTeam, isProcessing);
      const replies = out.filter((item) => item.type === 'message' && item.message.role === 'assistant');
      assert.equal(replies.length, 3);
      for (const item of replies) assert.equal(item.hideMeta, false);
    }
  }
});

test('完整工具结果在后续简报到来后仍独立显示，普通中间回应保持折叠', () => {
  for (const isTeam of [false, true]) {
    for (const isProcessing of [false, true]) {
      const first = assistantMessage(U + 2_000, U + 2_000, 'result-1');
      first.message.presentation = 'tool_result';
      const second = assistantMessage(U + 3_000, U + 3_000, 'result-2');
      second.message.presentation = 'tool_result';
      const out = buildRenderItems([
        userMessage(U),
        assistantMessage(U + 1_000, U + 1_000, 'ack'),
        first,
        second,
        assistantMessage(U + 4_000, U + 4_000, 'brief'),
      ], isTeam, isProcessing);
      const messages = out.filter((item) => item.type === 'message');
      assert.equal(messages.find((item) => item.message.id === 'result-1').hideMeta, false);
      assert.equal(messages.find((item) => item.message.id === 'result-2').hideMeta, false);
      assert.equal(messages.find((item) => item.message.id === 'ack').hideMeta, true);
      if (!isProcessing) assert.equal(messages.find((item) => item.message.id === 'brief').hideMeta, false);
    }
  }
});

test('异步工具结果不把它前面的普通最终回答变成中间过程', () => {
  const result = assistantMessage(U + 3_000, U + 3_000, 'result');
  result.message.presentation = 'tool_result';
  const out = buildRenderItems([
    userMessage(U),
    assistantMessage(U + 2_000, U + 2_000, 'answer'),
    result,
  ], false, false);
  for (const item of out.filter((item) => item.type === 'message' && item.message.role === 'assistant')) {
    assert.equal(item.hideMeta, false);
  }
});

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

function assistantMessage(ms, completedAt = ms, id = 'a1') {
  return {
    type: 'message',
    key: id,
    timestampMs: ms,
    sourceIndex: 1,
    message: {
      id,
      role: 'assistant',
      content: 'answer',
      timestamp: iso(ms),
      completedAt: iso(completedAt),
    },
  };
}

function commandOutputMessage(ms, id = 'cmd1') {
  return {
    type: 'message',
    key: id,
    timestampMs: ms,
    sourceIndex: 2,
    message: {
      id,
      role: 'system',
      content: '/compact\ncontext compressed',
      timestamp: iso(ms),
      isCommandOutput: true,
      commandName: 'compact',
    },
  };
}

function turnSummaryOf(items) {
  return items.find((item) => item.type === 'turnSummary');
}

function execution({ status, startedAt, updatedAt, agentTemplateName }) {
  return {
    toolCallId: `tc-${startedAt}`,
    toolCall: { id: `tc-${startedAt}`, name: 'bash', arguments: {} },
    status,
    startedAt: iso(startedAt),
    updatedAt: iso(updatedAt),
    timeoutAt: iso(startedAt + 60_000),
    ...(agentTemplateName ? { agentTemplateName } : {}),
  };
}

test('tool-first group keeps the Web Agent identity for its avatar', () => {
  const items = [
    userMessage(U),
    {
      type: 'toolExecution',
      key: 'tc-agent',
      timestampMs: S,
      sourceIndex: 0,
      execution: execution({
        status: 'pending',
        startedAt: S,
        updatedAt: S,
        agentTemplateName: 'expert-a',
      }),
    },
  ];

  const toolGroup = buildRenderItems(items, false, true).find((item) => item.type === 'toolGroup');
  assert.equal(toolGroup?.agentTemplateName, 'expert-a');
});

test('adjacent reasoning keeps a later Agent identity when the first segment lacks one', () => {
  const items = [
    userMessage(U),
    reasoningItem({ id: 'rsn-first', text: 'first', startedAt: S, closed: true }),
    reasoningItem({
      id: 'rsn-second',
      text: 'second',
      startedAt: S + 1,
      closed: true,
      agentTemplateName: 'expert-a',
    }, 1),
  ];

  const reasoning = buildRenderItems(items, false, false).find((item) => item.type === 'reasoning');
  assert.equal(reasoning?.segment.agentTemplateName, 'expert-a');
});

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

test('任务用时行移动到本轮内容顶部：头像下第一行，并接管顶部头像', () => {
  const items = [
    userMessage(U),
    assistantMessage(U + 2_000, U + 8_000),
  ];
  const out = buildRenderItems(items, false, false);
  const summaryIndex = out.findIndex((item) => item.type === 'turnSummary');
  const assistantIndex = out.findIndex(
    (item) => item.type === 'message' && item.message.role === 'assistant',
  );

  assert.ok(summaryIndex >= 0, '仍应生成任务用时行');
  assert.ok(summaryIndex < assistantIndex, '时间行应排在本轮 assistant 内容之前（头像下第一行）');
  const summary = out[summaryIndex];
  assert.equal(summary.showAvatar, true, '时间行接管本轮顶部头像');
  const assistant = out[assistantIndex];
  assert.equal(assistant.showAvatar, false, '首条 assistant 内容不再重复画头像');
});

test('slash 命令结果自成时间线块，不把上一轮任务用时排到卡片下方', () => {
  const assistantAt = U + 2_000;
  const completedAt = U + 8_000;
  const items = [
    userMessage(U),
    assistantMessage(assistantAt, completedAt),
    commandOutputMessage(U + 12_000),
  ];

  const out = buildRenderItems(items, false, false);
  const summaryIndex = out.findIndex((item) => item.type === 'turnSummary');
  const commandIndex = out.findIndex(
    (item) => item.type === 'message' && item.message.isCommandOutput,
  );

  assert.ok(summaryIndex >= 0, '上一轮仍应显示任务用时');
  assert.ok(commandIndex >= 0, '命令卡片仍应渲染');
  assert.ok(summaryIndex < commandIndex, '上一轮任务用时必须出现在命令卡片上方');
  assert.equal(
    out.filter((item) => item.type === 'turnSummary').length,
    1,
    '命令卡片自身不应新增任务用时',
  );
});
