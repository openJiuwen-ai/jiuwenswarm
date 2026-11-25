import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

// useWebSocket 挂载依赖面太大（webClient/路由/App 上下文），无法在单测里实例化。
// 沿用 contextUsage.test.mjs 的源码锚定模式：把「终态处理器必须收口思考段」
// 钉成源码不变量，防止后续改动静默弄丢 closeReasoning 调用。
// 背景（B_20261119_1013_chat-terminal-reasoning-close）：chat.error 只关
// thinking 不封思考段，GATEWAY_FORWARD_FAILED 等错误后「思考中…」永久转圈。

const source = readFileSync(new URL('../src/hooks/useWebSocket.ts', import.meta.url), 'utf8');

function handlerBlock(eventName) {
  const marker = `webClient.on('${eventName}'`;
  const start = source.indexOf(marker);
  assert.ok(start >= 0, `handler for ${eventName} must exist`);
  const next = source.indexOf("webClient.on(", start + marker.length);
  return source.slice(start, next === -1 ? source.length : next);
}

test('chat.error 终态处理器收口思考段（防「思考中…」永久转圈）', () => {
  const block = handlerBlock('chat.error');
  assert.ok(
    block.includes('closeReasoning(sessionId, {'),
    'chat.error must close the reasoning segment: it is a turn-terminal event and ' +
    'GATEWAY_FORWARD_FAILED may never be followed by processing_status(false)',
  );
  assert.ok(
    block.includes('eventTimestampMs(payload)'),
    'closure timestamp must come from the event payload',
  );
  // 收口要在 setThinking(false) 之后、错误消息组装之前（覆盖 heartbeat 等提前 return 的分支）
  const setThinking = block.indexOf('setThinking(sessionId, false)');
  const close = block.indexOf('closeReasoning(sessionId, {');
  assert.ok(setThinking >= 0 && close > setThinking, 'closeReasoning must follow setThinking(false)');
});

test('chat.final 兜底分支收口思考段（processing_status 丢帧时的唯一收尾信号）', () => {
  const block = handlerBlock('chat.final');
  assert.ok(
    block.includes('closeReasoning(sessionId, {'),
    'chat.final fallback must close the reasoning segment when it was left open',
  );
});

test('harness.session_finished 终态处理器收口思考段', () => {
  const block = handlerBlock('harness.session_finished');
  assert.ok(
    block.includes('closeReasoning(sessionId, {'),
    'harness.session_finished must close the reasoning segment',
  );
});

test('chat.error 普通错误路径复位输入区 processing（防停止按钮卡在执行态）', () => {
  const block = handlerBlock('chat.error');
  // GATEWAY_FORWARD_FAILED 特例分支早就复位 processing；这里钉住的是「走到错误
  // 展示路径的普通错误」也必须复位——否则 processing_status(false) 丢帧时
  // isProcessing 永不复位，发送按钮一直显示停止/执行态（点它只会再发中断）。
  assert.ok(
    block.includes("setProcessing(sessionId, false)"),
    'chat.error must reset processing on the ordinary error display path',
  );
  // 复位必须带 chat.final 兜底同款守卫：团队模式不关（后端团队仍运行时抑制
  // final，交给 team.completed），Goal 活跃不关（可能只是问答子轮失败）。
  assert.ok(
    block.includes("?.mode !== 'team'") && block.includes('goalStillActive'),
    'processing reset must carry the team-mode and goal-active guards (chat.final fallback parity)',
  );
});
