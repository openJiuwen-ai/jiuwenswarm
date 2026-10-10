import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applyToolUpdatePayload,
  useChatStore,
} from '../node_modules/.cache/tool-update-handler/toolUpdateHandler.mjs';

test('chat.tool_update applies reviewer metadata without completing the tool', () => {
  const sessionId = 'tool-update-handler';
  const toolCallId = 'call-tool-update-handler';
  useChatStore.getState().ensureRuntime(sessionId);

  try {
    useChatStore.getState().addToolCall(sessionId, {
      id: toolCallId,
      name: 'bash',
      arguments: { command: 'echo ok' },
    });
    applyToolUpdatePayload(sessionId, {
      tool_update: {
        tool_call_id: toolCallId,
        tool_name: 'bash',
        reviewer_metadata: {
          reviewer_status: 'approved',
          final_reviewer_status: 'approved',
          decision_source: 'auto_reviewer',
        },
      },
    });

    const execution = useChatStore
      .getState()
      .getRuntime(sessionId)
      .toolExecutions.get(toolCallId);
    assert.equal(execution.status, 'pending');
    assert.equal(execution.result, undefined);
    assert.equal(execution.toolCall.reviewer.final_reviewer_status, 'approved');
    assert.equal(execution.toolCall.reviewer.decision_source, 'auto_reviewer');
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});
