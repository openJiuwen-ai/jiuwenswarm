import assert from 'node:assert/strict';
import test from 'node:test';

import { useChatStore } from '../node_modules/.cache/chat-store-streaming/chatStore.mjs';

test('setThinking does not notify subscribers when the value is unchanged', () => {
  const sessionId = 'streaming-thinking-noop';
  useChatStore.getState().ensureRuntime(sessionId);
  let notifications = 0;
  const unsubscribe = useChatStore.subscribe(() => {
    notifications += 1;
  });

  try {
    useChatStore.getState().setThinking(sessionId, false);
    assert.equal(notifications, 0);

    useChatStore.getState().setThinking(sessionId, true);
    assert.equal(notifications, 1);

    useChatStore.getState().setThinking(sessionId, true);
    assert.equal(notifications, 1);
  } finally {
    unsubscribe();
    useChatStore.getState().removeRuntime(sessionId);
  }
});

test('accepted supplemental input splits reasoning only when a later reasoning delta arrives', () => {
  const sessionId = 'reasoning-supplement-boundary';
  const store = useChatStore.getState();
  store.ensureRuntime(sessionId);
  store.setProcessing(sessionId, true);
  store.setActiveExecutionId(sessionId, 'execution-A');
  store.appendReasoning(sessionId, 'original reasoning', { atMs: 1_700_000_001_000 });
  store.addToTaskQueue(sessionId, 'updated requirement');
  const taskId = store.getRuntime(sessionId).taskQueue[0].id;
  store.claimTaskInput(sessionId, taskId);
  store.bindTaskInputRequest(sessionId, taskId, 'supplement-request');
  store.settleTaskInput(sessionId, taskId, 'supplement-request', 'accepted');

  let runtime = store.getRuntime(sessionId);
  assert.equal(runtime.reasoningSegments.length, 1);
  assert.equal(runtime.reasoningSegments[0].closed, false, 'acceptance alone must not close reasoning');
  assert.equal(runtime.reasoningInputBoundaryPending, true);

  store.appendReasoning(sessionId, 'revised reasoning', { atMs: 1_700_000_002_000 });
  runtime = store.getRuntime(sessionId);
  assert.equal(runtime.reasoningSegments.length, 2);
  assert.equal(runtime.reasoningSegments[0].text, 'original reasoning');
  assert.equal(runtime.reasoningSegments[0].closed, true);
  assert.equal(runtime.reasoningSegments[1].text, 'revised reasoning');
  assert.equal(runtime.reasoningSegments[1].closed, false);
  assert.equal(runtime.reasoningInputBoundaryPending, false);

  store.removeRuntime(sessionId);
});

test('collapsed Agent final keeps the selected Agent identity', () => {
  const sessionId = 'streaming-agent-identity';
  useChatStore.getState().ensureRuntime(sessionId);
  useChatStore.getState().addMessage(sessionId, {
    id: 'user-identity',
    role: 'user',
    content: 'question',
    timestamp: '2026-08-31T10:00:00.000Z',
  });
  useChatStore.getState().addMessage(sessionId, {
    id: 'assistant-identity',
    role: 'assistant',
    content: 'partial',
    timestamp: '2026-08-31T10:00:01.000Z',
    isStreaming: true,
  });

  try {
    useChatStore.getState().collapseTurnFinal(sessionId, {
      kind: 'agent',
      content: 'complete',
      finalId: 'final-identity',
      timestampIso: '2026-08-31T10:00:02.000Z',
      agentTemplateName: 'expert-a',
    });
    const messages = useChatStore.getState().getRuntime(sessionId)?.messages ?? [];
    assert.equal(messages.at(-1)?.agentTemplateName, 'expert-a');
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});

test('collapsed Agent final rebinds supplemental input to the replacement message id', () => {
  const sessionId = 'streaming-supplement-final-rebind';
  const store = useChatStore.getState();
  store.ensureRuntime(sessionId);
  store.addMessage(sessionId, {
    id: 'user-original',
    role: 'user',
    content: 'write 500 words',
    timestamp: '2026-09-20T14:00:00.000Z',
  });
  store.addMessage(sessionId, {
    id: 'assistant-stream',
    role: 'assistant',
    content: 'partial answer',
    timestamp: '2026-09-20T14:00:01.000Z',
    isStreaming: true,
  });
  store.addMessage(sessionId, {
    id: 'user-supplement',
    role: 'user',
    content: 'change to 200 words',
    timestamp: '2026-09-20T14:00:02.000Z',
    supplementalInput: {
      executionId: 'execution-A',
      streamMessageId: 'assistant-stream',
      streamOffset: 8,
    },
  });

  try {
    store.collapseTurnFinal(sessionId, {
      kind: 'agent',
      content: 'complete answer',
      finalId: 'assistant-final',
      timestampIso: '2026-09-20T14:00:03.000Z',
    });
    const messages = store.getRuntime(sessionId).messages;
    assert.equal(
      messages.find((message) => message.id === 'user-supplement')
        ?.supplementalInput?.streamMessageId,
      'assistant-final',
    );
    assert.equal(messages.some((message) => message.id === 'assistant-stream'), false);
    assert.equal(messages.some((message) => message.id === 'assistant-final'), true);
  } finally {
    store.removeRuntime(sessionId);
  }
});

test('streaming reasoning keeps the selected Agent identity', () => {
  const sessionId = 'streaming-reasoning-identity';
  useChatStore.getState().ensureRuntime(sessionId);

  try {
    useChatStore.getState().appendReasoning(sessionId, 'thinking', {
      atMs: Date.parse('2026-08-31T10:00:01.000Z'),
      agentTemplateName: 'expert-a',
    });
    const segment = useChatStore.getState().getRuntime(sessionId)?.reasoningSegments.at(-1);
    assert.equal(segment?.agentTemplateName, 'expert-a');
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});

test('restored reasoning keeps the persisted Agent identity', () => {
  const sessionId = 'restored-reasoning-identity';
  useChatStore.getState().ensureRuntime(sessionId);

  try {
    useChatStore.getState().restoreReasoningSegments(sessionId, [{
      at: '2026-08-31T10:00:01.000Z',
      text: 'thinking',
      agentTemplateName: 'expert-a',
    }]);
    const segment = useChatStore.getState().getRuntime(sessionId)?.reasoningSegments.at(-1);
    assert.equal(segment?.agentTemplateName, 'expert-a');
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});

test('progressive history restore preserves existing reasoning segment ids', () => {
  const sessionId = 'restored-reasoning-stable-id';
  useChatStore.getState().ensureRuntime(sessionId);

  try {
    useChatStore.getState().restoreReasoningSegments(sessionId, [{
      at: '2026-08-31T10:00:02.000Z',
      text: 'visible reasoning',
      historyBatchSeq: 1,
    }]);
    const visible = useChatStore.getState().getRuntime(sessionId)?.reasoningSegments[0];
    assert.ok(visible?.id);

    useChatStore.getState().restoreReasoningSegments(sessionId, [
      {
        at: '2026-08-31T10:00:01.000Z',
        text: 'prefetched older reasoning',
        historyBatchSeq: 2,
      },
      {
        id: visible.id,
        at: '2026-08-31T10:00:02.000Z',
        text: visible.text,
        historyBatchSeq: visible.historyBatchSeq,
      },
    ]);

    const restored = useChatStore.getState().getRuntime(sessionId)?.reasoningSegments;
    assert.equal(restored?.find((segment) => segment.text === visible.text)?.id, visible.id);
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});

test('reviewer-only progress preserves the running tool lifecycle', () => {
  const sessionId = 'streaming-reviewer-progress';
  const toolCallId = 'call-reviewer-progress';
  useChatStore.getState().ensureRuntime(sessionId);

  try {
    useChatStore.getState().addToolCall(sessionId, {
      id: toolCallId,
      name: 'bash',
      arguments: { command: 'echo ok' },
    });
    const before = useChatStore.getState().getRuntime(sessionId).toolExecutions.get(toolCallId);

    useChatStore.getState().updateToolReviewer(sessionId, toolCallId, {
      reviewer_status: 'approved',
      final_reviewer_status: 'approved',
      decision_source: 'auto_reviewer',
    });

    const updated = useChatStore.getState().getRuntime(sessionId).toolExecutions.get(toolCallId);
    assert.equal(updated.status, 'pending');
    assert.equal(updated.result, undefined);
    assert.equal(updated.updatedAt, before.updatedAt);
    assert.equal(updated.toolCall.reviewer.final_reviewer_status, 'approved');

    useChatStore.getState().updateToolReviewer(sessionId, toolCallId, {
      reviewer_status: 'manual',
    });
    const late = useChatStore.getState().getRuntime(sessionId).toolExecutions.get(toolCallId);
    assert.equal(late.status, 'pending');
    assert.equal(late.result, undefined);
    assert.equal(late.updatedAt, before.updatedAt);
    assert.equal(late.toolCall.reviewer.final_reviewer_status, 'approved');

    useChatStore.getState().addToolResult(sessionId, {
      toolName: 'bash',
      toolCallId,
      result: 'command failed',
      success: false,
      reviewer: {
        reviewer_status: 'approved',
        final_reviewer_status: 'approved',
        decision_source: 'auto_reviewer',
      },
    });
    const failed = useChatStore.getState().getRuntime(sessionId).toolExecutions.get(toolCallId);
    assert.equal(failed.status, 'error');
    assert.equal(failed.result.success, false);
    assert.equal(failed.result.reviewer.final_reviewer_status, 'approved');
    const failedUpdatedAt = failed.updatedAt;

    useChatStore.getState().updateToolReviewer(sessionId, toolCallId, {
      reviewer_status: 'manual',
    });
    const afterLateUpdate = useChatStore.getState().getRuntime(sessionId).toolExecutions.get(toolCallId);
    assert.equal(afterLateUpdate.status, 'error');
    assert.equal(afterLateUpdate.result, failed.result);
    assert.equal(afterLateUpdate.updatedAt, failedUpdatedAt);
    assert.equal(afterLateUpdate.result.reviewer.final_reviewer_status, 'approved');

    useChatStore.getState().updateToolReviewer(sessionId, 'unknown-call', {
      final_reviewer_status: 'denied',
    });
    assert.equal(
      useChatStore.getState().getRuntime(sessionId).toolExecutions.has('unknown-call'),
      false,
    );
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});
