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
