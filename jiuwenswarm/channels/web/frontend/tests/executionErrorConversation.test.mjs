import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// useWebSocket 挂载依赖面太大，沿用 chatTerminalReasoningClose.test.mjs 的源码锚定。
// 普通对话的 execution.error 必须读 payload.message 并走 chat.error 同款收尾；
// Goal 快照和 Heartbeat 分支保持原有字段与收口。

const source = readFileSync(new URL('../src/hooks/useWebSocket.ts', import.meta.url), 'utf8');

function handlerBlock(eventName) {
  const marker = `webClient.on('${eventName}'`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `handler for ${eventName} must exist`);
  const next = source.indexOf("webClient.on(", start + marker.length);
  return source.slice(start, next === -1 ? source.length : next);
}

test('execution.error 普通对话展示 payload.message 并收尾 processing', () => {
  const block = handlerBlock('execution.error');
  const heartbeat = block.indexOf('if (hbErrorAutomation)');
  const message = block.indexOf('payload.message');
  assert.ok(heartbeat >= 0, 'heartbeat branch must stay');
  assert.ok(message > heartbeat, 'regular conversation reads payload.message after heartbeat');
  assert.ok(block.includes('applyGoalSnapshot(payload)'), 'goal snapshot branch must stay');
  assert.ok(
    block.includes('if (goal !== undefined && goal !== null) return;'),
    'a goal object must not fall through into the conversation error',
  );
  assert.ok(block.includes('closeHeartbeatSessionState(sessionId, hbErrorAutomation.run_id)'));
  assert.ok(block.includes('typeof payload.error === \'string\''), 'heartbeat still reads payload.error');
  assert.ok(block.includes('setProcessing(sessionId, false)'));
  assert.ok(block.includes("?.mode !== 'team'") && block.includes('goalWasActive'));
  assert.ok(block.includes('stopStreaming(sessionId)'));
  assert.ok(block.includes('closeReasoning(sessionId, {'));
  assert.ok(block.includes("t('network.errorPrefix', { message: errorMsg })"));
});
