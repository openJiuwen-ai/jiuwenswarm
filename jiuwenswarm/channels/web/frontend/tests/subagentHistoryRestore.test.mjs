import assert from 'node:assert/strict';
import test from 'node:test';

import {
  mergeHistoryToolReplayItems,
  parseHistoryJsonFileToTimelinePreview,
  parseSubagentHistoryReplay,
  recoverSubagentToolHistory,
  shouldProcessHistoryPayload,
  parseHistoryJsonFileToPreviewMessages,
} from '../node_modules/.cache/subagent-history/historyRestore.mjs';

const sessionId = 'web_session';
const subagentId = 'web_session_sub_general-purpose_1';

test('existing full-duplex spoken replies stay expanded after history restore without changing normal chat', () => {
  const messages = parseHistoryJsonFileToPreviewMessages([
    { id: 'ack', channel_id: 'video_duplex', role: 'assistant', event_type: 'chat.final', content: '好的，没问题，我现在就帮你生成这道题的代码。', timestamp: 1 },
    { id: 'receipt', channel_id: 'video_duplex', role: 'assistant', event_type: 'chat.final', content: '代码已生成。', timestamp: 2 },
    { id: 'normal', channel_id: 'web', role: 'assistant', event_type: 'chat.final', content: '普通对话。', timestamp: 3 },
  ], sessionId);
  assert.deepEqual(messages.map((message) => message.keepExpanded), [true, true, undefined]);
});

test('history restores an unmatched chat.ask_user_question as a read-only qa.summary card (no live re-prompt)', () => {
  const preview = parseHistoryJsonFileToTimelinePreview([
    {
      id: 'ask-1',
      role: 'assistant',
      event_type: 'chat.ask_user_question',
      content: '',
      request_id: 'req-ask-1',
      source: 'ask_user_interrupt',
      questions: [
        {
          question: '用哪种方案?',
          header: 'Question',
          options: [
            { label: '方案A', value: 'a', description: 'desc-a' },
            { label: '方案B', value: 'b' },
            { label: 'Other' },
          ],
          multi_select: false,
        },
      ],
      timestamp: 1,
    },
  ], sessionId);

  // 未答问题渲染成只读 qa.summary 卡片（QaSummaryCard 在 answers 为空时显示「—」），
  // 不进 pendingQuestions 弹框——web 重连后后端不重发挂起中断，弹框 + resume
  // 会报 "session has no active execution"，所以历史里的未答问题只读展示。
  assert.equal(preview.pendingQuestions.length, 0);
  assert.equal(preview.messages.length, 1);
  assert.equal(preview.messages[0].role, 'assistant');
  assert.equal(preview.messages[0].content.startsWith('qa.summary:'), true);
  const parsed = JSON.parse(preview.messages[0].content.slice('qa.summary:'.length));
  assert.deepEqual(parsed.items, [
    { header: 'Question', question: '用哪种方案?', answers: [] },
  ]);
});

test('history skips chat.ask_user_question with empty or missing questions', () => {
  const preview = parseHistoryJsonFileToTimelinePreview([
    {
      id: 'ask-empty',
      role: 'assistant',
      event_type: 'chat.ask_user_question',
      content: '',
      request_id: 'req-ask-empty',
      questions: [],
      timestamp: 1,
    },
    {
      id: 'ask-missing',
      role: 'assistant',
      event_type: 'chat.ask_user_question',
      content: '',
      request_id: 'req-ask-missing',
      timestamp: 2,
    },
  ], sessionId);

  assert.equal(preview.messages.length, 0);
  assert.equal(preview.pendingQuestions.length, 0);
});

test('history pairs chat.ask_user_answer with chat.ask_user_question by request_id, rendering a qa.summary card with answers', () => {
  const preview = parseHistoryJsonFileToTimelinePreview([
    {
      id: 'ask-1',
      role: 'assistant',
      event_type: 'chat.ask_user_question',
      content: '',
      request_id: 'req-pair-1',
      source: 'ask_user_interrupt',
      questions: [
        { question: '用哪种方案?', header: 'Question', options: [{ label: '方案A', value: 'a' }, { label: '方案B', value: 'b' }], multi_select: false },
        { question: '备注?', header: 'Question', options: [], multi_select: false },
      ],
      timestamp: 1,
    },
    {
      id: 'ans-1',
      role: 'assistant',
      event_type: 'chat.ask_user_answer',
      content: '',
      request_id: 'req-pair-1',
      source: 'ask_user_interrupt',
      answers: [
        { question: '用哪种方案?', selected_options: ['方案A'] },
        { question: '备注?', selected_options: [], custom_input: '加急' },
      ],
      timestamp: 2,
    },
  ], sessionId);

  // 已答：不弹框，进 messages 渲染成 qa.summary 回显卡
  assert.equal(preview.pendingQuestions.length, 0);
  assert.equal(preview.messages.length, 1);
  assert.equal(preview.messages[0].role, 'assistant');
  assert.equal(preview.messages[0].content.startsWith('qa.summary:'), true);
  const parsed = JSON.parse(preview.messages[0].content.slice('qa.summary:'.length));
  assert.deepEqual(parsed.items, [
    { header: 'Question', question: '用哪种方案?', answers: ['方案A'] },
    { header: 'Question', question: '备注?', answers: ['加急'] },
  ]);
  assert.equal(preview.answeredQuestions.length, 1);
  assert.equal(preview.answeredQuestions[0].payload.request_id, 'req-pair-1');
});

test('history leaves an unmatched chat.ask_user_question as a read-only qa.summary card (no answer record, no live prompt)', () => {
  const preview = parseHistoryJsonFileToTimelinePreview([
    {
      id: 'ask-unmatched',
      role: 'assistant',
      event_type: 'chat.ask_user_question',
      content: '',
      request_id: 'req-unmatched',
      source: 'ask_user_interrupt',
      questions: [{ question: 'Q?', header: 'Question', options: [{ label: 'A', value: 'a' }], multi_select: false }],
      timestamp: 1,
    },
  ], sessionId);

  // 未答：渲染成只读 qa.summary 卡片（answers 为空，QaSummaryCard 显示「—」），
  // 不进 pendingQuestions 弹框——web 重连后后端不重发挂起中断，弹框 + resume
  // 会报 "session has no active execution"，所以历史里的未答问题只读展示。
  assert.equal(preview.messages.length, 1);
  assert.equal(preview.messages[0].role, 'assistant');
  assert.equal(preview.messages[0].content.startsWith('qa.summary:'), true);
  const parsed = JSON.parse(preview.messages[0].content.slice('qa.summary:'.length));
  assert.deepEqual(parsed.items, [
    { header: 'Question', question: 'Q?', answers: [] },
  ]);
  assert.equal(preview.pendingQuestions.length, 0);
  assert.equal(preview.answeredQuestions.length, 0);
});

test('history sinks an unanswered qa.summary card to the turn end (after chat.final), not before it', () => {
  // 实时侧未答问题框是 LLM 那轮产出全部完成后才弹出（吸附输入框底部）。但历史里
  // chat.ask_user_question 的落盘时刻略早于同轮收尾的 chat.final（LLM 流式产出时
  // question chunk 先到、final 后到），直接按时间戳排序会把未答 qa.summary 卡排到
  // chat.final 前面，与实时反过来。历史恢复必须把未答卡沉到本轮末尾（chat.final
  // 之后），与实时位置一致。
  // 用真实 epoch 毫秒（sink 函数靠 timestampMsToIso 重新盖章，小 timestamp 会被
  // looksLikePlausibleEpochMs 判为不合理而跳过，无法验证 sink 行为）。
  const base = 1789975000000; // 2026 年附近
  const preview = parseHistoryJsonFileToTimelinePreview([
    {
      id: 'ask-unmatched-2',
      role: 'assistant',
      event_type: 'chat.ask_user_question',
      content: '',
      request_id: 'req-unmatched-2',
      source: 'ask_user_interrupt',
      questions: [{ question: 'Q?', header: 'Question', options: [{ label: 'A', value: 'a' }], multi_select: false }],
      timestamp: base,
    },
    {
      id: 'final-2',
      role: 'assistant',
      event_type: 'chat.final',
      content: '好的，我来确认一下。',
      request_id: 'req-turn-2',
      timestamp: base + 1000,
    },
  ], sessionId);

  // 期望顺序：[chat.final, qa.summary 卡]——qa.summary 卡沉到本轮末尾。
  // 旧逻辑（卡用 question 时间 base）会得到 [qa.summary 卡, chat.final]，与实时反。
  assert.equal(preview.messages.length, 2);
  assert.equal(preview.messages[0].content, '好的，我来确认一下。');
  assert.equal(preview.messages[1].content.startsWith('qa.summary:'), true);
  const parsed = JSON.parse(preview.messages[1].content.slice('qa.summary:'.length));
  assert.deepEqual(parsed.items, [
    { header: 'Question', question: 'Q?', answers: [] },
  ]);
  assert.equal(preview.pendingQuestions.length, 0);
  assert.equal(preview.answeredQuestions.length, 0);
});

test('history places an answered qa.summary card at the answer-time position, not the question-time position', () => {
  // 实时回显的 qa.summary 卡在用户点确认那一刻追加到列表末尾（答案提交时间），
  // 而非问题出现时刻。历史恢复必须一致：已答卡用 chat.ask_user_answer 的落盘时间
  // 定位，而非 chat.ask_user_question 的。否则卡会出现在问题后面、后续消息前面，
  // 与实时回显位置不一致。
  const preview = parseHistoryJsonFileToTimelinePreview([
    {
      id: 'ask-1',
      role: 'assistant',
      event_type: 'chat.ask_user_question',
      content: '',
      request_id: 'req-pos-1',
      source: 'ask_user_interrupt',
      questions: [{ question: '用哪种方案?', header: 'Question', options: [{ label: '方案A', value: 'a' }], multi_select: false }],
      timestamp: 1,
    },
    {
      id: 'mid-msg',
      role: 'assistant',
      event_type: 'chat.final',
      content: '中间这条消息不该被 qa.summary 卡越过',
      timestamp: 2,
    },
    {
      id: 'ans-1',
      role: 'assistant',
      event_type: 'chat.ask_user_answer',
      content: '',
      request_id: 'req-pos-1',
      source: 'ask_user_interrupt',
      answers: [{ question: '用哪种方案?', selected_options: ['方案A'] }],
      timestamp: 3,
    },
  ], sessionId);

  // 期望顺序：[中间消息(t2), qa.summary 卡(t3)]——qa.summary 卡在答案时间位置。
  // 旧逻辑（卡用问题时间 t1）会得到 [qa.summary 卡, 中间消息]，与实时回显不一致。
  assert.equal(preview.messages.length, 2);
  assert.equal(preview.messages[0].content, '中间这条消息不该被 qa.summary 卡越过');
  assert.equal(preview.messages[1].content.startsWith('qa.summary:'), true);
  const parsed = JSON.parse(preview.messages[1].content.slice('qa.summary:'.length));
  assert.deepEqual(parsed.items, [
    { header: 'Question', question: '用哪种方案?', answers: ['方案A'] },
  ]);
});

test('history restores accepted supplemental user input metadata', () => {
  const messages = parseHistoryJsonFileToPreviewMessages([
    {
      id: 'original:user',
      role: 'user',
      content: 'write 500 words',
      timestamp: 1,
    },
    {
      id: 'supplement:user',
      role: 'user',
      content: 'change to 200 words',
      timestamp: 2,
      is_supplemental_input: true,
      supplemental_input: {
        execution_id: 'execution-A',
        stream_offset: 0,
      },
    },
    {
      id: 'answer:assistant',
      role: 'assistant',
      event_type: 'chat.final',
      content: 'complete answer',
      timestamp: 3,
    },
  ], sessionId);

  assert.deepEqual(messages.map((message) => message.content), [
    'write 500 words',
    'change to 200 words',
    'complete answer',
  ]);
  assert.deepEqual(messages[1].supplementalInput, {
    executionId: 'execution-A',
    streamOffset: 0,
  });
});

test('history distinguishes a Core Agent result from Qwen acknowledgement and receipt', () => {
  const messages = parseHistoryJsonFileToPreviewMessages([
    { id: 'ack', role: 'assistant', event_type: 'chat.final', content: '我来处理。', timestamp: 1 },
    { id: 'result', role: 'assistant', event_type: 'chat.final', content: '```cpp\nint main() {}\n```', presentation: 'tool_result', timestamp: 2 },
    { id: 'receipt', role: 'assistant', event_type: 'chat.final', content: '代码已生成。', timestamp: 3 },
  ], sessionId);
  assert.equal(messages.length, 3);
  assert.deepEqual(messages.map((message) => message.presentation), [undefined, 'tool_result', undefined]);
  assert.equal(messages[1].content, '```cpp\nint main() {}\n```');
});

test('history restores the selected Agent identity from top-level and event payload fields', () => {
  const messages = parseHistoryJsonFileToPreviewMessages([
    {
      id: 'user-1',
      role: 'user',
      content: 'first',
      timestamp: '2026-08-31T10:00:00.000Z',
    },
    {
      id: 'final-a',
      role: 'assistant',
      event_type: 'chat.final',
      content: 'from A',
      agent_template_name: 'expert-a',
      timestamp: '2026-08-31T10:00:01.000Z',
    },
    {
      id: 'final-b',
      role: 'assistant',
      event_type: 'chat.final',
      content: 'from B',
      event_payload: { agent_template_name: 'expert-b' },
      timestamp: '2026-08-31T10:00:02.000Z',
    },
    {
      id: 'final-old',
      role: 'assistant',
      event_type: 'chat.final',
      content: 'legacy',
      timestamp: '2026-08-31T10:00:03.000Z',
    },
  ], 'web_session');

  assert.deepEqual(messages.map(({ id, agentTemplateName }) => ({ id, agentTemplateName })), [
    { id: 'user-1', agentTemplateName: undefined },
    { id: 'final-a', agentTemplateName: 'expert-a' },
    { id: 'final-b', agentTemplateName: 'expert-b' },
    { id: 'final-old', agentTemplateName: undefined },
  ]);
});

test('history restores the selected Agent identity on reasoning segments', () => {
  const preview = parseHistoryJsonFileToTimelinePreview([
    {
      id: 'final-with-reasoning',
      role: 'assistant',
      event_type: 'chat.final',
      content: 'answer',
      reasoning_content: 'thinking',
      agent_template_name: 'expert-a',
      timestamp: '2026-08-31T10:00:01.000Z',
    },
  ], sessionId);

  assert.equal(preview.reasoningSegments[0]?.agentTemplateName, 'expert-a');
});

test('subagent history replays persisted activity without treating it as final text', () => {
  const replay = parseSubagentHistoryReplay({
    role: 'assistant',
    event_type: 'chat.subagent_activity',
    timestamp: 1787019579.059,
    subagent_id: subagentId,
    content: 'search market data',
    subagent_activity: {
      subagent_id: subagentId,
      task_id: 'turn-1',
      seq: 4,
      kind: 'tool_call',
      summary: 'search market data',
      at_ms: 1787019579059,
      phase_id: 4,
      tool_name: 'web_search',
      tool_call_id: 'call-4',
    },
  }, sessionId, subagentId);

  assert.deepEqual(replay, {
    kind: 'activity',
    at: '2026-08-18T02:19:39.059Z',
    payload: {
      subagent_id: subagentId,
      task_id: 'turn-1',
      seq: 4,
      kind: 'tool_call',
      summary: 'search market data',
      at_ms: 1787019579059,
      phase_id: 4,
      tool_name: 'web_search',
      tool_call_id: 'call-4',
    },
  });
});

test('session history restores persisted usage summary onto the preceding assistant message', () => {
  const preview = parseHistoryJsonFileToTimelinePreview([
    {
      id: 'r1:user',
      role: 'user',
      timestamp: 1787019579,
      content: 'hello',
    },
    {
      id: 'r1:assistant',
      role: 'assistant',
      event_type: 'chat.final',
      timestamp: 1787019580,
      content: 'world',
    },
    {
      id: 'r1:assistant',
      role: 'assistant',
      event_type: 'chat.usage_summary',
      timestamp: 1787019581,
      content: '',
      usage: {
        input_tokens: 100,
        output_tokens: 20,
        total_tokens: 120,
        input_cost: 0.01,
        output_cost: 0.02,
        total_cost: 0.03,
      },
    },
  ], sessionId);

  assert.equal(preview.messages.length, 2);
  assert.deepEqual(preview.messages[1].usageSummary, {
    input_tokens: 100,
    output_tokens: 20,
    total_tokens: 120,
    input_cost: 0.01,
    output_cost: 0.02,
    total_cost: 0.03,
  });
});

test('session history restores the complete context usage payload without adding a chat message', () => {
  const contextUsage = {
    event_type: 'context.usage',
    schema_version: 'context-usage.v1',
    phase: 'post_call',
    request_id: 'context-request',
    product_session_id: sessionId,
    depth: 0,
    team_id: null,
    member_name: null,
    timestamp: '2026-08-18T02:19:41.000Z',
    context_window: {
      limit_tokens: 2000,
      input_tokens: 1000,
      occupancy_rate: 0.5,
      local_estimated_input_tokens: 231,
    },
    parts: {
      messages: {
        category: 'messages',
        tokens: 806,
        percentage_of_window: 0.403,
        source: 'provider_usage_residual',
      },
    },
    kv_cache: { session: { weighted_hit_rate: 0.6 } },
    session_kv_cache_hit_rate: 0.6,
    measurement: { tokenizer: 'unicode_codepoints', estimated: true },
  };
  const preview = parseHistoryJsonFileToTimelinePreview([
    {
      id: 'r2:user',
      role: 'user',
      timestamp: 1787019580,
      content: 'hello',
    },
    {
      id: 'r2:assistant',
      role: 'assistant',
      event_type: 'chat.final',
      timestamp: 1787019581,
      content: 'world',
    },
    {
      id: 'r2:context',
      role: 'assistant',
      event_type: 'context.usage',
      timestamp: 1787019582,
      content: '',
      ...contextUsage,
    },
  ], sessionId);

  assert.equal(preview.messages.length, 2);
  assert.equal(preview.contextUsageSnapshot.request_id, 'context-request');
  assert.deepEqual(preview.contextUsageSnapshot.context_window, contextUsage.context_window);
  assert.deepEqual(preview.contextUsageSnapshot.parts, contextUsage.parts);
  assert.deepEqual(preview.contextUsageSnapshot.kv_cache, contextUsage.kv_cache);
  assert.deepEqual(preview.contextUsageSnapshot.measurement, contextUsage.measurement);
});

test('team history restores the latest leader before a newer teammate frame', () => {
  const contextUsageRecord = ({ requestId, role, memberName, timestamp, inputTokens }) => ({
    id: requestId,
    role,
    mode: 'team',
    event_type: 'context.usage',
    request_id: requestId,
    product_session_id: sessionId,
    depth: role === 'leader' ? 2 : 0,
    team_id: 'runtime-team',
    member_name: memberName,
    timestamp,
    content: '',
    schema_version: 'context-usage.v1',
    phase: 'post_call',
    context_window: {
      limit_tokens: 2000,
      input_tokens: inputTokens,
      occupancy_rate: inputTokens / 2000,
    },
    parts: {},
    session_kv_cache_hit_rate: 0,
  });
  const leader = contextUsageRecord({
    requestId: 'leader-context',
    role: 'leader',
    memberName: 'explicit-leader-name',
    timestamp: '2026-09-03T03:00:00.000Z',
    inputTokens: 700,
  });
  const worker = contextUsageRecord({
    requestId: 'worker-context',
    role: 'teammate',
    memberName: 'worker-1',
    timestamp: '2026-09-03T03:01:00.000Z',
    inputTokens: 900,
  });

  const preview = parseHistoryJsonFileToTimelinePreview([leader, worker], sessionId);
  assert.equal(preview.mode, 'team');
  assert.equal(preview.contextUsageSnapshot.request_id, 'leader-context');
  assert.equal(preview.contextUsageSnapshot.role, 'leader');
  assert.equal(preview.contextUsageSnapshot.context_window.input_tokens, 700);

  const workerOnly = parseHistoryJsonFileToTimelinePreview([worker], sessionId);
  assert.equal(workerOnly.contextUsageSnapshot, null);
});

test('subagent history replays persisted roster status updates', () => {
  const replay = parseSubagentHistoryReplay({
    role: 'assistant',
    event_type: 'chat.subtask_update',
    timestamp: 1787019579.059,
    subagent_id: subagentId,
    parent_session_id: sessionId,
    status: 'idle',
    lifecycle: 'live',
    turn_outcome: 'completed',
    can_send_input: true,
    needs_resume: false,
    updated_at: 1787019579059,
    revision: 5,
  }, sessionId, subagentId);

  assert.equal(replay?.kind, 'updated');
  assert.equal(replay?.payload.status, 'idle');
  assert.equal(replay?.payload.revision, 5);
});

test('subagent roster updates keep the persisted display role', () => {
  const replay = parseSubagentHistoryReplay({
    role: '查询汕头今日天气',
    event_type: 'chat.subtask_update',
    timestamp: 1787019579.059,
    subagent_id: subagentId,
    parent_session_id: sessionId,
    display_name: '汕头天气查询员',
    task_description: '查询汕头今日天气',
    status: 'running',
    revision: 1,
  }, sessionId, subagentId);

  assert.equal(replay?.kind, 'updated');
  assert.equal(replay?.payload.display_name, '汕头天气查询员');
  assert.equal(replay?.payload.role, '查询汕头今日天气');
});

test('subagent history rejects activity whose nested parent session differs', () => {
  const replay = parseSubagentHistoryReplay({
    role: 'assistant',
    event_type: 'chat.subagent_activity',
    timestamp: 1787019579.059,
    subagent_id: subagentId,
    subagent_activity: {
      subagent_id: subagentId,
      parent_session_id: 'other-session',
      task_id: 'turn-1',
      seq: 4,
      kind: 'thinking',
      summary: 'cross-session activity',
      at_ms: 1787019579059,
    },
  }, sessionId, subagentId);

  assert.equal(replay, null);
});

test('subagent history rejects final text whose nested parent session differs', () => {
  const replay = parseSubagentHistoryReplay({
    role: 'assistant',
    event_type: 'chat.final',
    timestamp: 1787019579.059,
    subagent_id: subagentId,
    event_payload: { parent_session_id: 'other-session' },
    content: 'cross-session final',
  }, sessionId, subagentId);

  assert.equal(replay, null);
});

test('subagent history rejects nested payload boundary aliases', () => {
  const finalReplay = parseSubagentHistoryReplay({
    role: 'assistant',
    event_type: 'chat.final',
    timestamp: 1787019579.059,
    subagent_id: subagentId,
    content: 'nested cross-session final',
    payload: { parentSessionId: 'other-session' },
  }, sessionId, subagentId);
  const activityReplay = parseSubagentHistoryReplay({
    role: 'assistant',
    event_type: 'chat.subagent_activity',
    timestamp: 1787019579.059,
    subagent_id: subagentId,
    content: 'nested cross-session activity',
    payload: {
      subagent_activity: {
        subagent_id: subagentId,
        task_id: 'turn-1',
        seq: 5,
        kind: 'thinking',
        summary: 'cross-session',
        at_ms: 1787019579059,
        parentSessionId: 'other-session',
      },
    },
  }, sessionId, subagentId);

  assert.equal(finalReplay, null);
  assert.equal(activityReplay, null);
});

test('subagent history rejects parent frames without its exact subagent id', () => {
  assert.equal(shouldProcessHistoryPayload({
    session_id: sessionId,
    subagent_id: '',
    page_idx: 1,
  }, sessionId, 1, false, subagentId), false);
  assert.equal(shouldProcessHistoryPayload({
    session_id: sessionId,
    page_idx: 1,
  }, sessionId, 1, false, subagentId), false);
  assert.equal(shouldProcessHistoryPayload({
    session_id: sessionId,
    subagent_id: subagentId,
    page_idx: 1,
  }, sessionId, 1, false, subagentId), true);
});

test('cursor history accepts only the exact request cursor', () => {
  assert.equal(shouldProcessHistoryPayload({
    session_id: sessionId,
    cursor: null,
  }, sessionId, undefined, false, undefined, null), true);
  assert.equal(shouldProcessHistoryPayload({
    session_id: sessionId,
    cursor: 'cursor-2',
  }, sessionId, undefined, false, undefined, 'cursor-1'), false);
  assert.equal(shouldProcessHistoryPayload({
    session_id: sessionId,
    cursor: 'cursor-1',
  }, sessionId, undefined, false, undefined, 'cursor-1'), true);
});

test('history accepts copied fork records while still rejecting unrelated sessions', () => {
  const forkedRecord = {
    role: 'assistant',
    event_type: 'chat.final',
    content: 'copied answer',
    session_id: 'source-session',
    event_payload: { parent_session_id: 'source-session' },
    forked_from: {
      session_id: 'source-session',
      original_id: 'source-answer',
    },
  };

  assert.equal(
    shouldProcessHistoryPayload(
      {
        session_id: sessionId,
        page_idx: 1,
        message: forkedRecord,
      },
      sessionId,
      1,
    ),
    true,
  );
  assert.equal(
    shouldProcessHistoryPayload(
      {
        session_id: sessionId,
        page_idx: 1,
        message: {
          ...forkedRecord,
          event_payload: { parent_session_id: 'unrelated-session' },
        },
      },
      sessionId,
      1,
    ),
    false,
  );
});

test('history marks the inherited side of a fork boundary', () => {
  const messages = parseHistoryJsonFileToPreviewMessages([
    {
      id: 'source-user',
      role: 'user',
      content: 'source question',
      timestamp: 1,
      forked_from: { session_id: 'source-session' },
    },
    {
      id: 'source-assistant',
      role: 'assistant',
      event_type: 'chat.final',
      content: 'source answer',
      timestamp: 2,
      forked_from: { session_id: 'source-session' },
    },
    {
      id: 'branch-user',
      role: 'user',
      content: 'branch question',
      timestamp: 3,
    },
  ], sessionId);

  assert.deepEqual(
    messages.map((message) => message.forkedFromSessionId),
    ['source-session', 'source-session', undefined],
  );
});

test('parent tool history recovers roster and structured wait result without tool-result transcript text', () => {
  const recovered = recoverSubagentToolHistory([
    {
      kind: 'tool_call',
      at: '2026-08-17T12:00:00.000Z',
      payload: {
        tool_call: {
          name: 'subagent_spawn',
          arguments: JSON.stringify({
            subagent_type: 'general-purpose',
            task_description: 'Return the unique phrase',
            display_name: 'Agent A',
            role: 'Researcher',
          }),
        },
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-17T12:00:01.000Z',
      payload: {
        tool_name: 'subagent_spawn',
        result: `success=True data={'subagent_id': '${subagentId}', 'task_id': 'turn-1', 'status': 'running'} error=None`,
      },
    },
    {
      kind: 'tool_call',
      at: '2026-08-17T12:00:01.500Z',
      payload: {
        tool_call: {
          name: 'subagent_send_input',
          arguments: JSON.stringify({
            subagent_id: subagentId,
            query: 'Follow-up query',
          }),
        },
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-17T12:00:01.750Z',
      payload: {
        tool_name: 'subagent_send_input',
        result: `success=True data={'subagent_id': '${subagentId}', 'task_id': 'follow-up-task', 'status': 'running'} error=None`,
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-17T12:00:02.000Z',
      payload: {
        tool_name: 'subagent_wait',
        result: `success=True data={'statuses': {'${subagentId}': 'completed'}, 'results': {'${subagentId}': 'RESTORED_RESULT'}, 'output_files': {'${subagentId}': '/tmp/result.md'}, 'timed_out': False} error=None`,
      },
    },
    {
      kind: 'tool_call',
      at: '2026-08-17T12:00:03.000Z',
      payload: {
        tool_call: {
          name: 'subagent_close',
          arguments: JSON.stringify({ subagent_id: subagentId }),
        },
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-17T12:00:04.000Z',
      payload: {
        tool_name: 'subagent_close',
        result: `success=True data={'subagent_id': '${subagentId}', 'previous_status': 'completed'} error=None`,
      },
    },
  ], sessionId);

  assert.equal(recovered.length, 1);
  assert.equal(recovered[0].subagent.subagent_id, subagentId);
  assert.equal(recovered[0].subagent.display_name, 'Agent A');
  assert.equal(recovered[0].subagent.role, 'Researcher');
  assert.equal(recovered[0].subagent.status, 'closed');
  assert.equal(recovered[0].subagent.closed_reason, 'manual');
  assert.equal(recovered[0].subagent.task_description, 'Follow-up query');
  assert.deepEqual(recovered[0].turns, [
    { task_id: 'turn-1', task_description: 'Return the unique phrase', started_at: 1786968001000 },
    { task_id: 'follow-up-task', task_description: 'Follow-up query', started_at: 1786968001500 },
  ]);
  assert.deepEqual(recovered[0].result, {
    subagent_id: subagentId,
    content: 'RESTORED_RESULT',
    output_file: '/tmp/result.md',
  });
});

test('parent tool recovery pairs spawn calls and results across history pages', () => {
  const pageWithLatestFollowUp = [
    {
      kind: 'tool_call',
      at: '2026-08-21T01:56:15.000Z',
      payload: {
        tool_call: {
          name: 'subagent_send_input',
          arguments: JSON.stringify({ subagent_id: subagentId, query: 'Query tomorrow' }),
        },
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-21T01:56:16.000Z',
      payload: {
        tool_name: 'subagent_send_input',
        result: `success=True data={'subagent_id': '${subagentId}', 'task_id': 'turn-2', 'status': 'running'} error=None`,
      },
    },
  ];
  const pageWithEarlierSpawn = [
    {
      kind: 'tool_call',
      at: '2026-08-21T01:56:10.000Z',
      payload: {
        tool_call: {
          name: 'subagent_spawn',
          arguments: JSON.stringify({
            subagent_type: 'general-purpose',
            task_description: 'Query today',
            display_name: 'Agent A',
            role: 'Researcher',
          }),
        },
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-21T01:56:11.000Z',
      payload: {
        tool_name: 'subagent_spawn',
        result: `success=True data={'subagent_id': '${subagentId}', 'task_id': 'turn-1', 'status': 'running'} error=None`,
      },
    },
  ];

  const merged = mergeHistoryToolReplayItems(pageWithLatestFollowUp, pageWithEarlierSpawn);
  const recovered = recoverSubagentToolHistory(merged, sessionId);
  assert.deepEqual(recovered[0].turns, [
    { task_id: 'turn-1', task_description: 'Query today', started_at: 1787277371000 },
    { task_id: 'turn-2', task_description: 'Query tomorrow', started_at: 1787277375000 },
  ]);
});

test('history recovery ignores unknown states and failed close calls', () => {
  const unknown = recoverSubagentToolHistory([
    {
      kind: 'tool_result',
      at: '2026-08-17T12:01:00.000Z',
      payload: {
        tool_name: 'subagent_spawn',
        result: `success=True data={'subagent_id': '${subagentId}', 'status': 'running'} error=None`,
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-17T12:01:01.000Z',
      payload: {
        tool_name: 'subagent_wait',
        result: `success=True data={'statuses': {'${subagentId}': 'not_found'}, 'results': {}, 'output_files': {}, 'timed_out': False} error=None`,
      },
    },
  ], sessionId);
  assert.deepEqual(unknown, []);

  const failedClose = recoverSubagentToolHistory([
    {
      kind: 'tool_result',
      at: '2026-08-17T12:02:00.000Z',
      payload: {
        tool_name: 'subagent_spawn',
        result: `success=True data={'subagent_id': '${subagentId}', 'status': 'running'} error=None`,
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-17T12:02:01.000Z',
      payload: {
        tool_name: 'subagent_wait',
        result: `success=True data={'statuses': {'${subagentId}': 'completed'}, 'results': {}, 'output_files': {}, 'timed_out': False} error=None`,
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-17T12:02:02.000Z',
      payload: {
        tool_name: 'subagent_close',
        result: `success=False data=None error='cannot close running subagent: ${subagentId}'`,
      },
    },
  ], sessionId);
  assert.equal(failedClose[0].subagent.status, 'idle');
  assert.equal(failedClose[0].subagent.turn_outcome, 'completed');
  assert.equal(failedClose[0].subagent.closed_reason, null);

  const missingSuccessClose = recoverSubagentToolHistory([
    {
      kind: 'tool_result',
      at: '2026-08-17T12:03:00.000Z',
      payload: {
        tool_name: 'subagent_spawn',
        result: `success=True data={'subagent_id': '${subagentId}', 'status': 'running'} error=None`,
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-17T12:03:01.000Z',
      payload: {
        tool_name: 'subagent_wait',
        result: `success=True data={'statuses': {'${subagentId}': 'completed'}, 'results': {}, 'output_files': {}, 'timed_out': False} error=None`,
      },
    },
    {
      kind: 'tool_result',
      at: '2026-08-17T12:03:02.000Z',
      payload: {
        tool_name: 'subagent_close',
        result: `data={'subagent_id': '${subagentId}', 'previous_status': 'completed'}`,
      },
    },
  ], sessionId);
  assert.equal(missingSuccessClose[0].subagent.status, 'idle');
  assert.equal(missingSuccessClose[0].subagent.closed_reason, null);
});


for (const [format, supplementalFields] of [
  ['legacy', { supplemental_input: { request_id: 'input-1', output_phase_id: 'p1', stream_offset: 0 } }],
  ['compact', { request_id: 'input-1', is_supplemental_input: true }],
]) {
  test(`${format} phase history preserves visible bubbles and reasoning while excluding old tails`, () => {
    const order = (sequence) => ({ request_id: 'original', sequence });
    const records = [
      { id: 'u1', role: 'user', content: 'original question', timestamp: 1800000000000 },
      { id: 'a1', role: 'assistant', event_type: 'chat.final', content: 'visible prefix', timestamp: 1800000001000,
        output_phase_id: 'p1', output_order: order(2), reasoning_content: 'same thought', reasoning_output_order: order(1) },
      { id: 'u2', role: 'user', content: 'new instruction', timestamp: 1800000001000,
        output_order: order(3), ...supplementalFields },
      { id: 'old', role: 'assistant', event_type: 'chat.final', content: 'HIDDEN TAIL', timestamp: 1800000001001,
        output_order: order(4), output_suppressed: true, reasoning_content: 'HIDDEN THOUGHT' },
      { id: 'a2', role: 'assistant', event_type: 'chat.final', content: 'new result', timestamp: 1800000002000,
        output_phase_id: 'p2', output_order: order(7), reasoning_content: 'same thought', reasoning_output_order: order(6) },
    ];
    const restored = parseHistoryJsonFileToTimelinePreview(records, sessionId);
    assert.deepEqual(restored.messages.map((m) => m.content), [
      'original question', 'visible prefix', 'new instruction', 'new result',
    ]);
    assert.equal(restored.messages[2].supplementalInput.requestId, 'input-1');
    assert.deepEqual(restored.messages.slice(1).map((m) => m.outputOrder.sequence), [2, 3, 7]);
    assert.deepEqual(restored.reasoningSegments.map((s) => [s.text, s.outputOrder.sequence]), [
      ['same thought', 1], ['same thought', 6],
    ]);
  });
}

test('history restores chat.error as a system message with the error text', () => {
  // 后端 interface.py:4040 把 chat.error 落盘为 role=assistant, event_type=chat.error,
  // content=str(data), error_type 在顶层（code 未落盘）。历史恢复转成 role=system 消息，
  // 与实时 useWebSocket.ts chat.error 分支对齐（实时加 t('network.errorPrefix') 前缀，
  // 历史层无 i18n，直接用错误原文）。刷新后错误不再凭空消失。
  const messages = parseHistoryJsonFileToPreviewMessages([
    {
      id: 'err-1:assistant',
      role: 'assistant',
      event_type: 'chat.error',
      content: '模型调用失败：超时',
      error_type: 'TimeoutError',
      timestamp: 1,
    },
  ], sessionId);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].role, 'system');
  assert.equal(messages[0].content, '模型调用失败：超时');
});

test('history skips chat.error with empty content', () => {
  const messages = parseHistoryJsonFileToPreviewMessages([
    {
      id: 'err-empty:assistant',
      role: 'assistant',
      event_type: 'chat.error',
      content: '',
      error_type: 'RuntimeError',
      timestamp: 1,
    },
  ], sessionId);
  assert.equal(messages.length, 0);
});
