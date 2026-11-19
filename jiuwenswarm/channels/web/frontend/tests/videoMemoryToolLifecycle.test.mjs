import assert from 'node:assert/strict';
import test from 'node:test';

import {
  normalizeToolResultPayload,
  parseHistoryJsonFileToTimelinePreview,
  useChatStore,
} from '../node_modules/.cache/video-memory-tools/videoMemoryToolExports.mjs';

const submitId = 'video-submit:target';
const statusId = 'video-status:target';
const submitted = { job_id: 'ing-video', status: 'pending', reused: false };
const succeeded = { job_id: 'ing-video', status: 'succeeded', unit_ids: ['memory-1'] };

function result(toolName, toolCallId, output) {
  return {
    tool_name: toolName,
    tool_call_id: toolCallId,
    raw_output: output,
    rendered_result: JSON.stringify(output),
  };
}

test('live submission completes before ingestion and stays complete after a status query', () => {
  const session = 'video-memory-live';
  const store = useChatStore.getState();
  store.ensureRuntime(session);
  try {
    store.addToolCall(session, { id: submitId, name: 'agent_memory_video_submit', arguments: {} });
    store.addToolResult(session, normalizeToolResultPayload(result('agent_memory_video_submit', submitId, submitted)));
    assert.equal(store.getRuntime(session).toolExecutions.get(submitId).status, 'completed');

    store.addToolCall(session, { id: statusId, name: 'agent_memory_video_status', arguments: {} });
    store.addToolResult(session, normalizeToolResultPayload(result('agent_memory_video_status', statusId, succeeded)));
    assert.equal(store.getRuntime(session).toolExecutions.get(statusId).status, 'completed');
    const submission = store.getRuntime(session).toolExecutions.get(submitId);
    assert.equal(submission.status, 'completed');
    assert.deepEqual(JSON.parse(submission.result.result), submitted);
  } finally {
    store.removeRuntime(session);
  }
});

test('history replay completes a submitted video while preserving its original job status', () => {
  const preview = parseHistoryJsonFileToTimelinePreview([
    { role: 'user', content: 'Remember video', timestamp: '2026-11-19T00:00:01Z' },
    {
      role: 'assistant', event_type: 'chat.tool_call', timestamp: '2026-11-19T00:00:02Z',
      event_payload: { tool_call: { tool_call_id: submitId, name: 'agent_memory_video_submit', arguments: {} } },
    },
    {
      role: 'assistant', event_type: 'chat.tool_result', timestamp: '2026-11-19T00:00:03Z',
      event_payload: result('agent_memory_video_submit', submitId, submitted),
    },
    { role: 'assistant', event_type: 'chat.final', content: 'Submitted', timestamp: '2026-11-19T00:00:04Z' },
  ], 'video-memory-history');
  assert.equal(preview.executions.length, 1);
  assert.equal(preview.executions[0].status, 'completed');
  assert.deepEqual(JSON.parse(preview.executions[0].result.result), submitted);
});
