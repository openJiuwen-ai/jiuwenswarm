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
