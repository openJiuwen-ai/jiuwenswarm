import assert from 'node:assert/strict';
import test from 'node:test';

import { useChatStore } from '../node_modules/.cache/chat-store-streaming/chatStore.mjs';

const SID = 'lifecycle-stopped-session';

function setupRunningTurn() {
  const store = useChatStore.getState();
  store.ensureRuntime(SID);
  store.setProcessing(SID, true);
  store.setOrganizationProcessing(SID, true);
  store.setThinking(SID, true);
  store.setPaused(SID, true);
  store.addMessage(SID, {
    id: 'assistant-live',
    role: 'assistant',
    content: 'streaming answer',
    timestamp: new Date().toISOString(),
    isStreaming: true,
  });
  store.addMessage(SID, {
    id: 'team-leader-live',
    role: 'assistant',
    content: 'team leader answer',
    timestamp: new Date().toISOString(),
    isStreaming: true,
  });
  store.startStreaming(SID, 'assistant-live');
  store.addToolCall(SID, {
    id: 'bash-call-1',
    name: 'bash',
    arguments: { command: 'sleep 300' },
  }, { startedAt: new Date().toISOString() });
}

function teardown() {
  useChatStore.getState().removeRuntime(SID);
}

test('settleLifecycleStoppedSession closes a turn stopped by archive/delete', () => {
  setupRunningTurn();
  try {
    const before = useChatStore.getState().getRuntime(SID);
    assert.equal(before.isProcessing, true);
    assert.equal(before.toolExecutions.get('bash-call-1').status, 'pending');

    useChatStore.getState().settleLifecycleStoppedSession(SID);

    const runtime = useChatStore.getState().getRuntime(SID);
    assert.equal(runtime.foregroundIsProcessing, false);
    assert.equal(runtime.organizationIsProcessing, false);
    assert.equal(runtime.isProcessing, false);
    assert.equal(runtime.isThinking, false);
    assert.equal(runtime.isPaused, false);
    assert.equal(runtime.currentStreamId, null);
    assert.equal(runtime.messages.find((msg) => msg.id === 'assistant-live').isStreaming, false);
    assert.equal(runtime.messages.find((msg) => msg.id === 'team-leader-live').isStreaming, false);
    assert.equal(runtime.toolExecutions.get('bash-call-1').status, 'completed');
    assert.equal(typeof runtime.interruptSettledEpoch, 'number');
  } finally {
    teardown();
  }
});

test('settleLifecycleStoppedSession ignores sessions without a local runtime', () => {
  assert.doesNotThrow(() => {
    useChatStore.getState().settleLifecycleStoppedSession('missing-lifecycle-session');
  });
});
