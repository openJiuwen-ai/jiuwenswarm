import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

import {
  buildTimelineItems,
  buildLiveCompletedStreaks,
  buildRenderItems,
  buildTurnWorkMeta,
  buildTurnFoldAnchorKeys,
} from '../node_modules/.cache/build-turn-timeline/buildTurnTimeline.js';

const U = 1_700_000_000_000; // 用户消息时刻
const S = 1_700_000_005_000; // reasoning 首帧
const A = 1_700_000_035_000; // reasoning 末帧（updatedAt）

test('supplements stay after already visible assistant output', () => {
  const messages = [
    { id: 'user', role: 'user', content: 'write a story', timestamp: new Date(U).toISOString() },
    {
      id: 'answer',
      role: 'assistant',
      content: 'before-middle-after',
      timestamp: new Date(S).toISOString(),
      completedAt: new Date(A).toISOString(),
    },
    {
      id: 'extra-1',
      role: 'user',
      content: 'space theme',
      timestamp: new Date(S + 1000).toISOString(),
      supplementalInput: { executionId: 'execution', streamMessageId: 'answer', streamOffset: 7 },
    },
    {
      id: 'extra-2',
      role: 'user',
      content: 'happy ending',
      timestamp: new Date(S + 2000).toISOString(),
      supplementalInput: { executionId: 'execution', streamMessageId: 'answer', streamOffset: 14 },
    },
  ];
  const original = structuredClone(messages);
  for (const running of [true, false]) {
    const items = buildRenderItems(buildTimelineItems(messages, [], []), false, running);
    assert.deepEqual(
      items.filter((item) => item.type === 'message').map((item) => item.message.content),
      ['write a story', 'before-middle-after', 'space theme', 'happy ending'],
    );
    assert.equal(
      items.filter((item) => item.type === 'message' && item.message.role === 'assistant').length,
      1,
      'assistant 回复必须保持为一个连续气泡',
    );
    const summaries = items.filter((item) => item.type === 'turnSummary');
    assert.equal(summaries.length, running ? 2 : 1);
    assert.equal(summaries[0].startMs, U);
    assert.equal(summaries[0].endMs, A);
    assert.deepEqual(
      [
        ...new Set(
          items
            .filter((item) => item.type === 'message' && item.message.role === 'assistant')
            .map((item) => item.turnId),
        ),
      ],
      [1],
    );
  }
  assert.deepEqual(messages, original);
});

test('a supplement before the first assistant output stays above the timer and assistant block', () => {
  const messages = [
    { id: 'user', role: 'user', content: 'write a story', timestamp: new Date(U).toISOString() },
    {
      id: 'answer',
      role: 'assistant',
      content: 'complete answer',
      timestamp: new Date(S + 2_000).toISOString(),
      completedAt: new Date(A).toISOString(),
    },
    {
      id: 'early-extra',
      role: 'user',
      content: 'space theme',
      timestamp: new Date(S).toISOString(),
      supplementalInput: { executionId: 'execution', streamMessageId: 'answer', streamOffset: 0 },
    },
  ];

  for (const running of [true, false]) {
    const items = buildRenderItems(
      buildTimelineItems(
        messages,
        [],
        [
          {
            id: 'reasoning-after-supplement',
            text: 'thinking',
            startedAt: S + 1_000,
            updatedAt: S + 1_500,
            closedAt: S + 1_500,
            closed: true,
          },
        ],
      ),
      false,
      running,
    );
    const originalUserIndex = items.findIndex((item) => item.type === 'message' && item.message.id === 'user');
    const supplementIndex = items.findIndex((item) => item.type === 'message' && item.message.id === 'early-extra');
    const summaryIndex = items.findIndex((item) => item.type === 'turnSummary');
    const reasoningIndex = items.findIndex((item) => item.type === 'reasoning');
    const assistantIndex = items.findIndex((item) => item.type === 'message' && item.message.role === 'assistant');

    assert.ok(originalUserIndex < supplementIndex, '补充 USER 消息应位于原 USER 消息之后');
    assert.ok(supplementIndex < summaryIndex, '首包前的补充 USER 消息应位于计时行之前');
    assert.ok(summaryIndex < reasoningIndex, '计时行应紧邻 Jiuwen 工作内容区域顶部');
    assert.ok(reasoningIndex < assistantIndex, '首包前补充消息不得把 assistant 回复提前到思考之前');
    assert.equal(items[summaryIndex].showAvatar, true, '计时行应接管 assistant 顶部头像');
    assert.equal(items[assistantIndex].showAvatar, false, 'assistant 内容不应重复显示头像');
  }
});

test('a supplement without a stream id preserves the earlier answer before revised reasoning', () => {
  const messages = [
    { id: 'user', role: 'user', content: 'write 500 words', timestamp: new Date(U).toISOString() },
    {
      id: 'answer',
      role: 'assistant',
      content: 'complete answer',
      timestamp: new Date(S).toISOString(),
      completedAt: new Date(A).toISOString(),
    },
    {
      id: 'mid-extra',
      role: 'user',
      content: 'change to 200 words',
      timestamp: new Date(S + 1_000).toISOString(),
      supplementalInput: { executionId: 'execution', streamOffset: 0 },
    },
  ];
  const items = buildRenderItems(
    buildTimelineItems(
      messages,
      [],
      [
        {
          id: 'reasoning-after-steer',
          text: 'revise to 200 words',
          startedAt: S + 2_000,
          updatedAt: S + 3_000,
          closedAt: S + 3_000,
          closed: true,
        },
      ],
    ),
    false,
    false,
  );
  const supplementIndex = items.findIndex((item) => item.type === 'message' && item.message.id === 'mid-extra');
  const reasoningIndex = items.findIndex((item) => item.type === 'reasoning');
  const assistantIndex = items.findIndex((item) => item.type === 'message' && item.message.id === 'answer');

  assert.ok(supplementIndex < reasoningIndex);
  assert.ok(assistantIndex < supplementIndex, '已展示的正文必须保留在补充消息之前');
  assert.equal(
    items.filter((item) => item.type === 'message' && item.message.id === 'answer').length,
    1,
    'assistant 最终回答仍应保持为一个完整气泡',
  );
});

test('late acceptance stays in its original turn and an empty continuation retains final metadata', () => {
  const messages = [
    { id: 'user', role: 'user', content: 'first', timestamp: new Date(U).toISOString() },
    {
      id: 'answer',
      role: 'assistant',
      content: 'answer',
      timestamp: new Date(S).toISOString(),
      completedAt: new Date(A).toISOString(),
      fileItems: [{ name: 'result.txt' }],
    },
    { id: 'next-user', role: 'user', content: 'second', timestamp: new Date(A + 1000).toISOString() },
    {
      id: 'next-answer',
      role: 'assistant',
      content: 'second answer',
      timestamp: new Date(A + 2000).toISOString(),
      isStreaming: true,
    },
    {
      id: 'late',
      role: 'user',
      content: 'extra',
      timestamp: new Date(A + 3000).toISOString(),
      supplementalInput: { executionId: 'old-execution', streamMessageId: 'answer', streamOffset: 6 },
    },
  ];
  const items = buildRenderItems(buildTimelineItems(messages, [], []), false, true);
  assert.deepEqual(
    items.filter((item) => item.type === 'message').map((item) => item.message.content),
    ['first', 'answer', 'second', 'second answer', 'extra'],
  );
  const summaries = items.filter((item) => item.type === 'turnSummary');
  assert.equal(summaries.length, 3);
  assert.equal(summaries[0].endMs, A);
  assert.equal(summaries[1].startMs, A + 1000);
  assert.deepEqual(items.find((item) => item.type === 'message' && item.message.id === 'answer').message.fileItems, [
    { name: 'result.txt' },
  ]);
});

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

test('完整工具结果在后续简报到来后仍独立显示，普通中间回应隐藏元信息', () => {
  for (const isTeam of [false, true]) {
    for (const isProcessing of [false, true]) {
      const first = assistantMessage(U + 2_000, U + 2_000, 'result-1');
      first.message.presentation = 'tool_result';
      const second = assistantMessage(U + 3_000, U + 3_000, 'result-2');
      second.message.presentation = 'tool_result';
      const out = buildRenderItems(
        [
          userMessage(U),
          assistantMessage(U + 1_000, U + 1_000, 'ack'),
          first,
          second,
          assistantMessage(U + 4_000, U + 4_000, 'brief'),
        ],
        isTeam,
        isProcessing,
      );
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
  const out = buildRenderItems(
    [userMessage(U), assistantMessage(U + 2_000, U + 2_000, 'answer'), result],
    false,
    false,
  );
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

function turnSummaryKeys(items) {
  return items.filter((item) => item.type === 'turnSummary').map((item) => item.key);
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
    reasoningItem(
      {
        id: 'rsn-second',
        text: 'second',
        startedAt: S + 1,
        closed: true,
        agentTemplateName: 'expert-a',
      },
      1,
    ),
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
  const items = [userMessage(U), assistantMessage(U + 2_000, U + 8_000)];
  const out = buildRenderItems(items, false, false);
  const summaryIndex = out.findIndex((item) => item.type === 'turnSummary');
  const assistantIndex = out.findIndex((item) => item.type === 'message' && item.message.role === 'assistant');

  assert.ok(summaryIndex >= 0, '仍应生成任务用时行');
  assert.ok(summaryIndex < assistantIndex, '时间行应排在本轮 assistant 内容之前（头像下第一行）');
  const summary = out[summaryIndex];
  assert.equal(summary.showAvatar, true, '时间行接管本轮顶部头像');
  const assistant = out[assistantIndex];
  assert.equal(assistant.showAvatar, false, '首条 assistant 内容不再重复画头像');
});

test('assistant 早于折叠工作时，任务用时条仍由顶部 summary 锚点渲染', () => {
  const out = buildRenderItems(
    [
      userMessage(U),
      assistantMessage(U + 2_000, U + 3_000),
      reasoningItem({
        id: 'reasoning-after-answer',
        text: 'follow-up work',
        startedAt: S,
        updatedAt: A,
        closedAt: A,
        closed: true,
      }),
    ],
    false,
    false,
  );
  const summaryIndex = out.findIndex((item) => item.type === 'turnSummary');
  const assistantIndex = out.findIndex((item) => item.type === 'message' && item.message.role === 'assistant');
  const meta = buildTurnWorkMeta(out, false).get(1);

  assert.ok(meta?.completed && meta.hasWork, '该轮应渲染可折叠任务用时条');
  assert.ok(summaryIndex < assistantIndex, '任务用时条的锚点必须位于 assistant 正文之前');

  const source = readFileSync(new URL('../src/components/ChatPanel/MessageList.tsx', import.meta.url), 'utf8');
  const workRenderBranch = source
    .split("if (item.type === 'reasoning' || item.type === 'toolGroup') {")
    .at(-1)
    .split("if (item.type === 'turnSummary') {")[0];
  const summaryRenderBranch = source.split("if (item.type === 'turnSummary') {").at(-1);
  assert.match(summaryRenderBranch, /return renderTurnChip\(turnKey, meta\)/);
  assert.doesNotMatch(workRenderBranch, /renderTurnChip/);
});

test('slash 命令结果自成时间线块，不把上一轮任务用时排到卡片下方', () => {
  const assistantAt = U + 2_000;
  const completedAt = U + 8_000;
  const items = [userMessage(U), assistantMessage(assistantAt, completedAt), commandOutputMessage(U + 12_000)];

  const out = buildRenderItems(items, false, false);
  const summaryIndex = out.findIndex((item) => item.type === 'turnSummary');
  const commandIndex = out.findIndex((item) => item.type === 'message' && item.message.isCommandOutput);

  assert.ok(summaryIndex >= 0, '上一轮仍应显示任务用时');
  assert.ok(commandIndex >= 0, '命令卡片仍应渲染');
  assert.ok(summaryIndex < commandIndex, '上一轮任务用时必须出现在命令卡片上方');
  assert.equal(out.filter((item) => item.type === 'turnSummary').length, 1, '命令卡片自身不应新增任务用时');
});

test('历史前插完整回合时，既有任务用时行保持原有 key', () => {
  const current = [
    userMessage(U, 'u10'),
    assistantMessage(U + 1_000, U + 2_000, 'a10'),
    userMessage(U + 10_000, 'u11'),
    assistantMessage(U + 11_000, U + 12_000, 'a11'),
  ];
  const before = buildRenderItems(current, false, false);
  const after = buildRenderItems(
    [userMessage(U - 10_000, 'u9'), assistantMessage(U - 9_000, U - 8_000, 'a9'), ...current],
    false,
    false,
  );

  assert.deepEqual(turnSummaryKeys(before), ['turn-summary-a10', 'turn-summary-a11']);
  assert.deepEqual(
    turnSummaryKeys(after),
    ['turn-summary-a9', 'turn-summary-a10', 'turn-summary-a11'],
    '前插只能新增旧回合 key，既有回合 key 不得整体改号',
  );
});

test('历史补齐首个半回合时，边界回合的任务用时 key 也保持不变', () => {
  const knownTail = [
    assistantMessage(U + 1_000, U + 2_000, 'a10'),
    userMessage(U + 10_000, 'u11'),
    assistantMessage(U + 11_000, U + 12_000, 'a11'),
  ];
  const before = buildRenderItems(knownTail, false, false);
  const after = buildRenderItems([userMessage(U, 'u10'), ...knownTail], false, false);

  assert.deepEqual(turnSummaryKeys(before), ['turn-summary-a10', 'turn-summary-a11']);
  assert.deepEqual(
    turnSummaryKeys(after),
    ['turn-summary-a10', 'turn-summary-a11'],
    '补齐边界回合后，所有既有 key 都必须保持不变',
  );
});

test('只有用户消息的进行中回合仍显示任务用时，并锚定该用户消息', () => {
  const out = buildRenderItems([userMessage(U, 'u-running')], false, true);
  const summary = turnSummaryOf(out);

  assert.ok(summary, '进行中回合仍应显示任务用时');
  assert.equal(summary.key, 'turn-summary-u-running');
});

test('历史前插扩展同一 streak 时，展开态 key 锚定末项并保持不变', () => {
  const now = 1_800_000_000_000;
  const workItem = (key) => ({
    type: 'reasoning',
    key,
    showAvatar: false,
    turnId: 7,
    segment: {
      id: key,
      text: key,
      startedAt: now - 20_000,
      updatedAt: now - 15_000,
      closedAt: now - 10_000,
      closed: true,
    },
  });
  const before = [...buildLiveCompletedStreaks([workItem('r2'), workItem('r3')], now).values()];
  const after = [...buildLiveCompletedStreaks([workItem('r1'), workItem('r2'), workItem('r3')], now).values()];

  assert.equal(before[0].firstKey, 'r2');
  assert.equal(after[0].firstKey, 'r1');
  assert.equal(before[0].id, 'streak-r3');
  assert.equal(after[0].id, 'streak-r3');
});

test('正文不参与工作折叠，后续总结不会把澄清问题收起', () => {
  for (const isTeam of [false, true]) {
    for (const isProcessing of [false, true]) {
      const question = assistantMessage(U + 1_000, U + 1_000, 'questions');
      question.message.content = '请补充岗位方向、背景和面试时间。';
      const items = buildRenderItems(
        [
          userMessage(U),
          question,
          reasoningItem({ id: 'reasoning', text: '等待用户回复', startedAt: S, closed: true }),
          assistantMessage(A, A, 'summary'),
        ],
        isTeam,
        isProcessing,
      );
      const anchors = buildTurnFoldAnchorKeys(items, buildTurnWorkMeta(items, isProcessing));
      assert.equal(anchors.get(1), isProcessing ? undefined : 'reasoning');
      assert.equal(items.find((item) => item.key === 'questions').message.content, question.message.content);
    }
  }
  // 渲染层不得再用 hideMeta 将正文放进折叠容器。
  const source = readFileSync(new URL('../src/components/ChatPanel/MessageList.tsx', import.meta.url), 'utf8');
  const messageBranch = source
    .split("if (item.type === 'message') {")
    .at(-1)
    .split("if (item.type === 'reasoning' || item.type === 'toolGroup') {")[0];
  assert.ok(messageBranch.includes('<MessageItem'));
  assert.ok(messageBranch.includes('renderAfterMessage?.(item.message)'));
  assert.doesNotMatch(messageBranch, /timeline-collapse|turnFoldable/);
});
