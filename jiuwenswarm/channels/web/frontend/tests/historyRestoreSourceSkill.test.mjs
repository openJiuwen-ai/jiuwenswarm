import assert from 'node:assert/strict';
import test from 'node:test';

import { parseHistoryJsonFileToTimelinePreview } from '../node_modules/.cache/history-restore/historyRestore.mjs';

test('history replay restores failed turns as system error messages', () => {
  const { messages } = parseHistoryJsonFileToTimelinePreview([
    { role: 'user', content: 'create a task', timestamp: 1757800000 },
    { role: 'assistant', event_type: 'chat.error', content: 'cron failed', timestamp: 1757800001 },
    { role: 'assistant', event_type: 'chat.error', content: '', error: 'route failed', timestamp: 1757800002 },
    { role: 'assistant', event_type: 'chat.error', event_payload: { error: 'agent failed' }, timestamp: 1757800003 },
    { role: 'assistant', event_type: 'chat.error', content: '  ', timestamp: 1757800004 },
    { role: 'assistant', event_type: 'chat.final', content: 'retry succeeded', timestamp: 1757800005 },
  ], 'error-session');
  assert.equal(messages.length, 5);
  assert.equal(messages[1].role, 'system');
  assert.match(messages[1].content, /cron failed/);
  assert.equal(messages[2].role, 'system');
  assert.match(messages[2].content, /route failed/);
  assert.equal(messages[3].role, 'system');
  assert.match(messages[3].content, /agent failed/);
  assert.equal(messages[4].role, 'assistant');
  assert.equal(messages[4].content, 'retry succeeded');
});

// 历史记录字段对齐 pod 本地 history.json 的落盘结构：chat.tool_call 事件把
// tool_call（含 source_skill）展平在 extra 里（interface.py _append_history_record）。
const TOOL_CALL_WITH_SOURCE_SKILL = {
  role: 'assistant',
  request_id: 'rid-1',
  channel_id: 'web',
  timestamp: 1757800000,
  event_type: 'chat.tool_call',
  content: '',
  tool_call: {
    id: 'tc-1',
    name: 'web_search',
    arguments: { query: 'jiuwenswarm' },
    source_skill: 'data-analysis',
  },
};

const TOOL_RESULT_RECORD = {
  role: 'assistant',
  request_id: 'rid-1',
  channel_id: 'web',
  timestamp: 1757800001,
  event_type: 'chat.tool_result',
  content: '',
  tool_result: {
    tool_name: 'web_search',
    tool_call_id: 'tc-1',
    result: 'ok',
    success: true,
  },
};

test('history replay keeps source_skill on the rebuilt tool call', () => {
  const { executions } = parseHistoryJsonFileToTimelinePreview(
    [TOOL_CALL_WITH_SOURCE_SKILL, TOOL_RESULT_RECORD],
    'session-1',
  );

  const execution = executions.find((e) => e.toolCallId === 'tc-1');
  assert.ok(execution, 'tool execution should be rebuilt from history');
  assert.equal(execution.toolCall.source_skill, 'data-analysis');
  // 实时链路依赖的其余字段也一并在场，确认重建口径完整。
  assert.equal(execution.toolCall.name, 'web_search');
  assert.equal(execution.status, 'completed');
});

test('history replay without source_skill stays undefined (no fake value)', () => {
  const withoutSkill = {
    ...TOOL_CALL_WITH_SOURCE_SKILL,
    tool_call: {
      id: 'tc-2',
      name: 'web_search',
      arguments: {},
    },
  };
  const { executions } = parseHistoryJsonFileToTimelinePreview(
    [withoutSkill],
    'session-2',
  );

  const execution = executions.find((e) => e.toolCallId === 'tc-2');
  assert.ok(execution);
  assert.equal(execution.toolCall.source_skill, undefined);
});
