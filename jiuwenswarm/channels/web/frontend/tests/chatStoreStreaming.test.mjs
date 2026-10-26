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

test('stopStreaming seals the current bubble so later appends do not mix turns', () => {
  const sessionId = 'overlap-send-seal';
  const store = useChatStore.getState();
  store.ensureRuntime(sessionId);
  try {
    store.addMessage(sessionId, {
      id: 'asst-1',
      role: 'assistant',
      content: 'first',
      timestamp: new Date().toISOString(),
      isStreaming: true,
    });
    store.startStreaming(sessionId, 'asst-1');
    store.stopStreaming(sessionId);

    const sealed = useChatStore.getState().getRuntime(sessionId);
    assert.equal(sealed.currentStreamId, null);
    assert.equal(sealed.messages.find((message) => message.id === 'asst-1').isStreaming, false);

    store.appendStreamContent(sessionId, ' leaked');
    const after = useChatStore.getState().getRuntime(sessionId);
    assert.equal(after.messages.find((message) => message.id === 'asst-1').content, 'first');
  } finally {
    useChatStore.getState().removeRuntime(sessionId);
  }
});
