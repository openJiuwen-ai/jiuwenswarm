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

test('replaceHistoryMessages keeps a pending approval prompt armed', () => {
  // Reattach flow: the backend republishes the parked tool-approval ask on
  // session.switch, which can land BEFORE history restore finishes. The
  // restore's replaceHistoryMessages must not wipe pendingQuestion, or the
  // republished prompt dies again and the parked tool call waits forever.
  const sessionId = 'restore-pending-question';
  useChatStore.getState().ensureRuntime(sessionId);
  const pending = {
    request_id: 'chatcmpl-tool-parked-1',
    source: 'permission_interrupt',
    questions: [
      {
        question: 'allow clouddoc_batch_edit?',
        options: [{ label: '本次允许' }, { label: '拒绝' }],
        multi_select: false,
      },
    ],
  };

  try {
    useChatStore.getState().enqueuePendingQuestion(sessionId, pending);
    useChatStore.getState().replaceHistoryMessages(sessionId, [
      {
        id: 'hist-user-1',
        role: 'user',
        content: 'edit the doc',
        timestamp: '2026-09-04T06:27:00.000Z',
      },
    ]);

    const runtime = useChatStore.getState().runtimes[sessionId];
    assert.equal(runtime.messages.length, 1);
    assert.deepEqual(runtime.pendingQuestions, [pending]);
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});

// --- the permission interrupt's synthetic failure -------------------------
// When a tool call is gated by an approval prompt, the host's resilience rail
// turns the propagating interrupt into a failed tool_result and streams it
// BEFORE the ask. Nothing failed: the call is parked waiting for the person.
// Two orderings have to survive it, and both did flash "执行失败" in the UI.

const askFor = (toolCallId) => ({
  request_id: toolCallId,
  source: 'permission_interrupt',
  questions: [
    {
      question: 'allow clouddoc_batch_edit?',
      options: [{ label: '本次允许' }, { label: '拒绝' }],
      multi_select: false,
    },
  ],
});

test('a failed result under a pending ask never becomes an orphan failure', () => {
  // Ordering A (the one seen live): result, then ask, then the resumed run's
  // tool_call. Without the guard the tool call adopts the orphan and renders
  // as failed until the real result lands.
  const sessionId = 'interrupt-orphan-drop';
  const toolCallId = 'chatcmpl-tool-flash-a';
  useChatStore.getState().ensureRuntime(sessionId);

  try {
    useChatStore.getState().enqueuePendingQuestion(sessionId, askFor(toolCallId));
    useChatStore.getState().addToolResult(sessionId, {
      toolName: 'clouddoc_batch_edit',
      result: "success=False data=None error=''",
      success: false,
      toolCallId,
    });
    assert.equal(useChatStore.getState().getRuntime(sessionId)?.orphanResults.size, 0);

    useChatStore.getState().addToolCall(sessionId, {
      id: toolCallId,
      name: 'clouddoc_batch_edit',
      arguments: {},
    });
    const execution = useChatStore.getState().getRuntime(sessionId)?.toolExecutions.get(toolCallId);
    assert.equal(execution?.status, 'pending', 'an approved call must not open as failed');

    useChatStore.getState().clearPendingQuestions(sessionId);
    useChatStore.getState().addToolResult(sessionId, {
      toolName: 'clouddoc_batch_edit',
      result: "{'ok': True, 'receipt_id': 'abc'}",
      success: true,
      toolCallId,
    });
    assert.equal(
      useChatStore.getState().getRuntime(sessionId)?.toolExecutions.get(toolCallId)?.status,
      'completed',
    );
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});

test('a failed result on an executing call under a pending ask stays pending', () => {
  // Ordering B: the tool_call arrived first, so the synthetic failure lands on
  // an execution that already exists.
  const sessionId = 'interrupt-execution-hold';
  const toolCallId = 'chatcmpl-tool-flash-b';
  useChatStore.getState().ensureRuntime(sessionId);

  try {
    useChatStore.getState().addToolCall(sessionId, {
      id: toolCallId,
      name: 'clouddoc_batch_edit',
      arguments: {},
    });
    useChatStore.getState().enqueuePendingQuestion(sessionId, askFor(toolCallId));
    useChatStore.getState().addToolResult(sessionId, {
      toolName: 'clouddoc_batch_edit',
      result: "success=False data=None error=''",
      success: false,
      toolCallId,
    });
    assert.equal(
      useChatStore.getState().getRuntime(sessionId)?.toolExecutions.get(toolCallId)?.status,
      'pending',
      'a call waiting on the person has not failed',
    );
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});

test('an ask arriving after the failure clears what the failure left behind', () => {
  // Ordering C: the failure is already recorded when the ask arrives, either as
  // an orphan or on an execution marked error. Both are undone by the ask.
  const sessionId = 'interrupt-late-ask';
  const orphanId = 'chatcmpl-tool-flash-c1';
  const executingId = 'chatcmpl-tool-flash-c2';
  useChatStore.getState().ensureRuntime(sessionId);

  try {
    useChatStore.getState().addToolResult(sessionId, {
      toolName: 'clouddoc_batch_edit',
      result: 'boom',
      success: false,
      toolCallId: orphanId,
    });
    assert.equal(useChatStore.getState().getRuntime(sessionId)?.orphanResults.size, 1);
    useChatStore.getState().enqueuePendingQuestion(sessionId, askFor(orphanId));
    assert.equal(
      useChatStore.getState().getRuntime(sessionId)?.orphanResults.size,
      0,
      'the ask says the orphan failure was the interrupt, not an outcome',
    );

    useChatStore.getState().addToolCall(sessionId, {
      id: executingId,
      name: 'clouddoc_batch_edit',
      arguments: {},
    });
    useChatStore.getState().addToolResult(sessionId, {
      toolName: 'clouddoc_batch_edit',
      result: 'boom',
      success: false,
      toolCallId: executingId,
    });
    assert.equal(
      useChatStore.getState().getRuntime(sessionId)?.toolExecutions.get(executingId)?.status,
      'error',
    );
    useChatStore.getState().enqueuePendingQuestion(sessionId, askFor(executingId));
    assert.equal(
      useChatStore.getState().getRuntime(sessionId)?.toolExecutions.get(executingId)?.status,
      'pending',
      'the ask reopens a call that was marked failed by the interrupt',
    );
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});

test('a genuine failure outside any ask is still a failure', () => {
  const sessionId = 'interrupt-guard-scope';
  const toolCallId = 'chatcmpl-tool-real-failure';
  useChatStore.getState().ensureRuntime(sessionId);

  try {
    useChatStore.getState().enqueuePendingQuestion(sessionId, askFor('some-other-call'));
    useChatStore.getState().addToolCall(sessionId, {
      id: toolCallId,
      name: 'clouddoc_batch_edit',
      arguments: {},
    });
    useChatStore.getState().addToolResult(sessionId, {
      toolName: 'clouddoc_batch_edit',
      result: 'the platform said no',
      success: false,
      toolCallId,
    });
    assert.equal(
      useChatStore.getState().getRuntime(sessionId)?.toolExecutions.get(toolCallId)?.status,
      'error',
      'the guard is keyed to the ask, not to failure in general',
    );
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});
