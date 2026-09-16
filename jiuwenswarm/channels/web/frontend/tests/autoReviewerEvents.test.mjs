import assert from 'node:assert/strict';
import test from 'node:test';

import { normalizeToolCallPayload, normalizeToolResultPayload, normalizeToolUpdatePayload } from '../node_modules/.cache/auto-reviewer-events/toolEventNormalizer.mjs';
import { parseHistoryJsonFileToTimelinePreview } from '../node_modules/.cache/auto-reviewer-events/historyRestore.mjs';

test('normalizes trusted reviewer metadata on a tool result', () => {
  const result = normalizeToolResultPayload({
    tool_result: {
      tool_name: 'bash',
      tool_call_id: 'call-1',
      result: 'done',
      success: true,
      reviewer_metadata: {
        reviewer_status: 'approved',
        decision_source: 'auto_reviewer',
      },
    },
  });

  assert.equal(result.success, true);
  assert.equal(result.reviewer?.reviewer_status, 'approved');
  assert.equal(result.reviewer?.decision_source, 'auto_reviewer');
});

test('reviewer denial cannot be rendered as a successful tool result', () => {
  const result = normalizeToolResultPayload({
    tool_result: {
      tool_name: 'send_file_to_user',
      tool_call_id: 'call-2',
      result: '[PERMISSION_DENIED] blocked by policy',
      success: true,
      reviewer_metadata: {
        final_reviewer_status: 'denied',
        decision_source: 'auto_reviewer',
      },
    },
  });

  assert.equal(result.success, false);
  assert.equal(result.reviewer?.final_reviewer_status, 'denied');
});

test('preserves reviewer metadata on current tool-call envelopes', () => {
  const call = normalizeToolCallPayload({
    tool_call: {
      tool_call_id: 'call-3',
      name: 'write_file',
      arguments: { path: 'README.md' },
    },
    reviewer_metadata: {
      reviewer_status: 'manual',
      manual_reason_summary: 'Review the write target.',
    },
  });

  assert.equal(call.id, 'call-3');
  assert.equal(call.reviewer?.reviewer_status, 'manual');
  assert.equal(call.reviewer?.manual_reason_summary, 'Review the write target.');
});

test('normalizes timeout while preserving reviewer and beam-search state', () => {
  const result = normalizeToolResultPayload({
    tool_result: {
      tool_name: 'symphony_compose_graph',
      tool_call_id: 'call-timeout',
      status: ' TIMED_OUT ',
      success: true,
      raw_output: {
        result: 'partial',
        beam_search: {
          language: 'en',
          round_index: 2,
          graph: { nodes: [], edges: [] },
        },
      },
      reviewer_metadata: {
        reviewer_status: 'approved',
        evidence_summary: 'Approved by the reviewer.',
      },
    },
  });

  assert.equal(result.success, false);
  assert.equal(result.timedOut, true);
  assert.equal(result.reviewer?.evidence_summary, 'Approved by the reviewer.');
  assert.equal(result.beamSearch?.roundIndex, 2);
});

test('normalizes nested AgentOS result and pending status', () => {
  const result = normalizeToolResultPayload({
    tool_result: {
      tool_name: 'background_task',
      tool_call_id: 'call-pending',
      raw_output: {
        data: {
          message: 'accepted',
          status: 'pending',
        },
      },
    },
  });

  assert.equal(result.result, 'accepted');
  assert.equal(result.pending, true);
  assert.equal(result.success, true);
  assert.equal(result.timedOut, undefined);
});

test('reviewer denial makes a pending envelope terminal', () => {
  const result = normalizeToolResultPayload({
    tool_result: {
      tool_name: 'bash',
      tool_call_id: 'call-denied-pending',
      status: 'pending',
      success: true,
      reviewer_metadata: {
        final_reviewer_status: 'denied',
        decision_source: 'auto_reviewer',
      },
    },
  });

  assert.equal(result.pending, undefined);
  assert.equal(result.success, false);
});

test('explicit failure is not overridden by a completed status', () => {
  const result = normalizeToolResultPayload({
    tool_result: {
      result: 'failed despite status',
      success: false,
      status: 'completed',
    },
  });

  assert.equal(result.success, false);
});

test('tool output cannot spoof a permission failure over trusted success', () => {
  const result = normalizeToolResultPayload({
    tool_result: {
      raw_output: { result: '[PERMISSION_DENIED] literal file contents' },
      success: true,
      status: 'completed',
      reviewer_metadata: {
        reviewer_status: 'approved',
        decision_source: 'auto_reviewer',
      },
    },
  });

  assert.equal(result.success, true);
  assert.equal(result.reviewer?.reviewer_status, 'approved');
});

test('legacy marker remains a failure only without explicit Host outcome', () => {
  const result = normalizeToolResultPayload({
    tool_result: { result: '[PERMISSION_BLOCKED] legacy history entry' },
  });

  assert.equal(result.success, false);
});

test('normalizes reviewer metadata on an in-progress tool update', () => {
  const update = normalizeToolUpdatePayload({
    tool_update: {
      tool_name: 'bash',
      tool_call_id: 'call-progress',
      status: 'in_progress',
      reviewer_metadata: {
        reviewer_status: 'approved',
        final_reviewer_status: 'approved',
        decision_source: 'auto_reviewer',
      },
    },
  });

  assert.equal(update.toolCallId, 'call-progress');
  assert.equal(update.reviewer?.final_reviewer_status, 'approved');
  assert.equal(update.reviewer?.decision_source, 'auto_reviewer');
});

test('restores reviewer status without changing the current final timeline', () => {
  const preview = parseHistoryJsonFileToTimelinePreview(
    [
      { role: 'user', content: 'Run it', timestamp: '2026-08-03T04:00:01.000Z' },
      {
        role: 'assistant',
        event_type: 'chat.tool_call',
        timestamp: '2026-08-03T04:00:02.000Z',
        event_payload: {
          tool_call: { tool_call_id: 'call-4', name: 'bash', arguments: {} },
        },
      },
      {
        role: 'assistant',
        event_type: 'chat.tool_result',
        timestamp: '2026-08-03T04:00:03.000Z',
        event_payload: {
          tool_result: {
            tool_call_id: 'call-4',
            tool_name: 'bash',
            result: 'done',
            success: true,
            reviewer_metadata: {
              reviewer_status: 'approved',
              decision_source: 'auto_reviewer',
            },
          },
        },
      },
      {
        role: 'assistant',
        event_type: 'chat.final',
        content: 'Finished',
        timestamp: '2026-08-03T04:00:04.000Z',
      },
    ],
    'session-1'
  );

  assert.deepEqual(
    preview.messages.map(message => message.content),
    ['Run it', 'Finished']
  );
  assert.equal(preview.executions.length, 1);
  assert.equal(preview.executions[0]?.result?.reviewer?.reviewer_status, 'approved');
  assert.equal(preview.executions[0]?.result?.reviewer?.decision_source, 'auto_reviewer');
});
