import assert from 'node:assert/strict';
import test from 'node:test';

import { useChatStore } from '../node_modules/.cache/chat-store-streaming/chatStore.mjs';

const SID = 'settle-historical-test';

function setup() {
  useChatStore.getState().ensureRuntime(SID);
}

function teardown() {
  useChatStore.getState().removeRuntime(SID);
}

function addToolCall(toolCallId, toolName) {
  useChatStore.getState().addToolCall(SID, {
    id: toolCallId,
    name: toolName,
    arguments: {},
  }, { startedAt: new Date().toISOString() });
}

function getToolStatus(toolCallId) {
  const runtime = useChatStore.getState().getRuntime(SID);
  if (!runtime) return undefined;
  const execution = runtime.toolExecutions.get(toolCallId);
  return execution?.status;
}

test('settleHistoricalToolExecutions settles pending tools when isProcessing is false', () => {
  setup();
  try {
    addToolCall('call-1', 'list_files');
    assert.equal(getToolStatus('call-1'), 'pending');

    useChatStore.getState().settleHistoricalToolExecutions(SID);
    assert.equal(getToolStatus('call-1'), 'completed');
  } finally {
    teardown();
  }
});

test('settleHistoricalToolExecutions skips settling when isProcessing is true', () => {
  setup();
  try {
    useChatStore.getState().setProcessing(SID, true);
    addToolCall('call-2', 'code');

    useChatStore.getState().settleHistoricalToolExecutions(SID);
    assert.equal(getToolStatus('call-2'), 'pending');
  } finally {
    useChatStore.getState().setProcessing(SID, false);
    teardown();
  }
});

test('settleHistoricalToolExecutions settles after setProcessing false', () => {
  setup();
  try {
    useChatStore.getState().setProcessing(SID, true);
    addToolCall('call-3', 'cron');

    useChatStore.getState().settleHistoricalToolExecutions(SID);
    assert.equal(getToolStatus('call-3'), 'pending');

    useChatStore.getState().setProcessing(SID, false);
    useChatStore.getState().settleHistoricalToolExecutions(SID);
    assert.equal(getToolStatus('call-3'), 'completed');
  } finally {
    teardown();
  }
});

test('settleHistoricalToolExecutions does not downgrade completed tools', () => {
  setup();
  try {
    addToolCall('call-4', 'search');

    useChatStore.getState().addToolResult(SID, {
      toolCallId: 'call-4',
      toolName: 'search',
      result: 'found',
      success: true,
    });
    assert.equal(getToolStatus('call-4'), 'completed');

    useChatStore.getState().settleHistoricalToolExecutions(SID);
    assert.equal(getToolStatus('call-4'), 'completed');
  } finally {
    teardown();
  }
});

test('settleHistoricalToolExecutions preserves timeout tools (does not rewrite as completed)', () => {
  setup();
  try {
    addToolCall('call-5', 'slow_tool');
    // 把 timeoutAt 拨到过去，触发巡检超时
    const runtime = useChatStore.getState().getRuntime(SID);
    const execution = runtime.toolExecutions.get('call-5');
    runtime.toolExecutions.set('call-5', {
      ...execution,
      timeoutAt: new Date(Date.now() - 1000).toISOString(),
    });

    useChatStore.getState().markTimedOutExecutions(SID);
    assert.equal(getToolStatus('call-5'), 'timeout');

    useChatStore.getState().settleHistoricalToolExecutions(SID);
    assert.equal(getToolStatus('call-5'), 'timeout');
  } finally {
    teardown();
  }
});

test('settleHistoricalToolExecutions preserves error tools', () => {
  setup();
  try {
    addToolCall('call-5b', 'fail_tool');
    useChatStore.getState().addToolResult(SID, {
      toolCallId: 'call-5b',
      toolName: 'fail_tool',
      result: 'boom',
      success: false,
    });
    assert.equal(getToolStatus('call-5b'), 'error');

    useChatStore.getState().settleHistoricalToolExecutions(SID);
    assert.equal(getToolStatus('call-5b'), 'error');
  } finally {
    teardown();
  }
});

test('addToolCall then addToolResult yields completed status', () => {
  setup();
  try {
    addToolCall('call-6', 'read_file');

    useChatStore.getState().addToolResult(SID, {
      toolCallId: 'call-6',
      toolName: 'read_file',
      result: 'content',
      success: true,
    });
    assert.equal(getToolStatus('call-6'), 'completed');
  } finally {
    teardown();
  }
});
